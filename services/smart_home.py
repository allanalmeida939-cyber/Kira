"""Casa inteligente pelo Wi-Fi: descobre tudo o que está na rede de casa e controla o que der.

Descoberta (sem senha, sem nuvem): varre a rede local (ping + tabela ARP do Windows), pergunta quem fala UPnP/SSDP (TVs
LG/Samsung, roteador, caixas de som) e quem anuncia por mDNS (Chromecast/Google, AirPlay, Spotify Connect, impressoras,
HomeKit). Junta tudo por IP: nome, fabricante, modelo e tipo. Celular com MAC privado aparece como "celular/tablet".
Controle: Chromecast e Google Nest (pychromecast: volume, pausa, tocar, parar) e TV LG webOS (aiowebostv: volume,
desligar, mudo; na 1ª vez a TV pede para aceitar na tela). O resto aparece na lista, só para ver.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import inspect
import json
import logging
import re
import socket
import subprocess
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

logger = logging.getLogger("kira.app.smart_home")

ROOT = Path(__file__).resolve().parents[1]
TV_LEARNED_PATH = ROOT / "configs" / "tv_learned.json"  # o que cada app da TV pede (ex.: pausa = OK, OK no Prime IPTV)


def tv_learned(app_id: str, action: str) -> list[str]:
    try:
        data = json.loads(TV_LEARNED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    buttons = ((data.get("apps") or {}).get(app_id) or {}).get(action) or []
    return [str(b) for b in buttons if str(b).isalnum()][:4]


def tv_learn(app_id: str, **actions: list[str]) -> None:
    try:
        data = json.loads(TV_LEARNED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("apps", {}).setdefault(app_id, {}).update(actions)
    TV_LEARNED_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("TV_LEARNED app=%s %s", app_id, actions)
STATE_FILE = ROOT / "configs" / "smart_home.json"  # nomes que o Chefe deu, chave da TV LG, último resultado
MDNS_SERVICES = {
    "_googlecast._tcp.local.": "Google / Chromecast", "_airplay._tcp.local.": "AirPlay", "_spotify-connect._tcp.local.": "Spotify Connect",
    "_ipp._tcp.local.": "Impressora", "_printer._tcp.local.": "Impressora", "_hap._tcp.local.": "HomeKit",
    "_androidtvremote2._tcp.local.": "Android TV", "_amzn-wplay._tcp.local.": "Fire TV", "_sonos._tcp.local.": "Sonos",
    "_ewelink._tcp.local.": "eWeLink (Sonoff)", "_hue._tcp.local.": "Philips Hue", "_matter._tcp.local.": "Matter",
}
# começo do MAC -> fabricante (os mais comuns em casa no Brasil); o resto pergunta a um serviço público, com cache
OUI = {"28:68:d2": "Huawei", "0c:79:55": "Intelbras", "58:96:0a": "LG", "a8:23:fe": "LG", "64:95:6c": "LG", "f4:f5:d8": "Google",
       "d8:6c:63": "Google", "48:d6:d5": "Google", "50:c7:bf": "TP-Link", "98:da:c4": "TP-Link", "8c:79:f5": "Samsung",
       "f8:3f:51": "Samsung", "44:23:7c": "Xiaomi", "78:11:dc": "Xiaomi", "dc:a6:32": "Raspberry Pi"}
SCAN_TIMEOUT_S = 6.0
LG_WAKE_CONFIRM_TIMEOUT_S = 8.0
LG_WAKE_POLL_INTERVAL_S = 1.0


# Botões do controle LG (o resto do controle é app: Netflix, Prime Video, Disney+, Globoplay, LG Channels, Alexa)
TV_BUTTONS = {"voltar": "BACK", "home": "HOME", "ok": "ENTER", "cima": "UP", "baixo": "DOWN", "esquerda": "LEFT", "direita": "RIGHT",
              "canal+": "CHANNELUP", "canal-": "CHANNELDOWN", "lista": "LIST", "info": "INFO", "menu": "MENU", "sair": "EXIT",
              **{str(n): str(n) for n in range(10)}}


@dataclass
class Device:
    ip: str
    mac: str = ""
    name: str = ""
    maker: str = ""
    model: str = ""
    kind: str = "aparelho"
    services: list[str] = field(default_factory=list)
    control: str = ""  # chromecast | lgtv | ""
    nickname: str = ""

    @property
    def label(self) -> str:
        # A descoberta LG pode anunciar o mesmo aparelho pelo SSDP e pelo mDNS.
        # Mantemos a integração técnica (control/services) no registro, mas usamos
        # um único nome estável para não apresentar "TV da Sala" e "LG webOS TV"
        # como se fossem aparelhos diferentes.
        if not self.nickname and self.control == "lgtv":
            return "TV da Sala"
        return self.nickname or self.name or (f"{self.maker} {self.model}".strip()) or self.ip


def private_mac(mac: str) -> bool:
    """MAC 'privado' (aleatório): bit de administração local ligado. Celulares e tablets fazem isso por padrão."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


def local_networks() -> list[ipaddress.IPv4Network]:
    nets = []
    try:
        import psutil

        for name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET and addr.netmask and addr.address.startswith(("192.168.", "10.", "172.")):
                    net = ipaddress.ip_network(f"{addr.address}/{addr.netmask}", strict=False)
                    if net.num_addresses <= 1024:
                        nets.append(net)
    except Exception:  # noqa: BLE001
        pass
    return nets


def arp_table() -> dict[str, str]:
    out = subprocess.run(["arp", "-a"], capture_output=True, text=True, encoding="latin-1", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    table = {}
    for ip, mac in re.findall(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{2}(?:-[0-9a-f]{2}){5})\s+din", out, re.IGNORECASE):
        table[ip] = mac.lower().replace("-", ":")
    return table


async def ping_sweep(net: ipaddress.IPv4Network) -> None:
    """Acorda a tabela ARP: um ping rápido em cada endereço da rede (em paralelo)."""
    sem = asyncio.Semaphore(64)

    async def one(ip: str) -> None:
        async with sem:
            proc = await asyncio.create_subprocess_exec("ping", "-n", "1", "-w", "300", ip, stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            await proc.wait()

    await asyncio.gather(*(one(str(host)) for host in net.hosts()))


def ssdp_search(timeout: float = 3.0) -> dict[str, str]:
    """IP -> endereço da descrição UPnP de quem respondeu."""
    msg = "\r\n".join(["M-SEARCH * HTTP/1.1", "HOST: 239.255.255.250:1900", 'MAN: "ssdp:discover"', "MX: 2", "ST: ssdp:all", "", ""]).encode()
    found: dict[str, str] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(0.5)
    try:
        sock.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, (ip, _) = sock.recvfrom(4096)
            except socket.timeout:
                continue
            location = re.search(rb"(?im)^location:\s*(\S+)", data)
            if location and ip not in found:
                found[ip] = location.group(1).decode(errors="ignore")
    finally:
        sock.close()
    return found


async def upnp_info(client: httpx.AsyncClient, url: str) -> dict[str, str]:
    try:
        root = ET.fromstring((await client.get(url, timeout=3.0)).content)
    except (httpx.HTTPError, ET.ParseError):
        return {}
    ns = {"d": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    dev = root.find("d:device", ns) if ns else root.find("device")
    if dev is None:
        return {}
    get = lambda tag: (dev.findtext(f"d:{tag}", default="", namespaces=ns) if ns else dev.findtext(tag, default="")).strip()  # noqa: E731
    return {"name": get("friendlyName"), "maker": get("manufacturer"), "model": get("modelName") or get("modelDescription"), "type": get("deviceType")}


def mdns_browse(seconds: float = 3.0) -> dict[str, dict[str, Any]]:
    """IP -> {nome, serviços} de quem se anuncia por mDNS."""
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    found: dict[str, dict[str, Any]] = {}

    class Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name, timeout=1500)
            if not info:
                return
            for ip in info.parsed_addresses():
                if ":" in ip:
                    continue
                entry = found.setdefault(ip, {"name": "", "services": [], "props": {}})
                props = {k.decode(errors="ignore"): (v or b"").decode(errors="ignore") for k, v in (info.properties or {}).items()}
                entry["name"] = entry["name"] or props.get("fn") or name.split("._")[0]
                entry["services"].append(MDNS_SERVICES.get(type_, type_))
                entry["props"].update({k: v for k, v in props.items() if k in {"md", "fn", "manufacturer"}})

        def update_service(self, *args: Any) -> None:
            pass

        def remove_service(self, *args: Any) -> None:
            pass

    zc = Zeroconf()
    try:
        browsers = [ServiceBrowser(zc, service, Listener()) for service in MDNS_SERVICES]
        time.sleep(seconds)
        del browsers
    finally:
        zc.close()
    return found


def classify(device: Device) -> None:
    text = f"{device.name} {device.maker} {device.model} {' '.join(device.services)}".lower()
    # TV LG antes do Chromecast: a LG webOS também anuncia Chromecast, mas pelo webOS dá para desligar e mudar o volume
    if "lg" in text and ("tv" in text or "webos" in text or "oled" in text):
        device.kind, device.control = "TV LG", "lgtv"
    elif "google / chromecast" in text or "chromecast" in text or "nest" in text:
        device.kind, device.control = ("caixa de som" if ("nest" in text or "home" in text) and "tv" not in text else "Chromecast / TV"), "chromecast"
    elif any(word in text for word in ("tv", "bravia", "tizen", "android tv", "fire tv", "roku")):
        device.kind = "TV"
    elif any(word in text for word in ("router", "roteador", "gateway", "huawei", "intelbras", "tp-link", "internetgateway")):
        device.kind = "roteador / Wi-Fi"
    elif "impressora" in text or "printer" in text:
        device.kind = "impressora"
    elif any(word in text for word in ("spotify", "sonos", "speaker", "airplay")):
        device.kind = "caixa de som"
    elif any(word in text for word in ("hue", "sonoff", "ewelink", "homekit", "matter", "tuya", "smart")):
        device.kind = "casa inteligente"
    elif device.mac and private_mac(device.mac):
        device.kind = "celular / tablet"


class SmartHome:
    def __init__(
        self,
        state_file: Path = STATE_FILE,
        *,
        wake_sender: Callable[[Device], Any] | None = None,
        wake_probe: Callable[[Device], Any] | None = None,
        wake_confirm_timeout_s: float = LG_WAKE_CONFIRM_TIMEOUT_S,
        wake_poll_interval_s: float = LG_WAKE_POLL_INTERVAL_S,
    ) -> None:
        self._file = Path(state_file)
        self.state: dict[str, Any] = self._load()
        self.devices: dict[str, Device] = {ip: Device(**d) for ip, d in (self.state.get("devices") or {}).items() if isinstance(d, dict)}
        self.scanned_at = float(self.state.get("scanned_at") or 0)
        self._scan_lock = asyncio.Lock()
        self._wake_sender = wake_sender
        self._wake_probe = wake_probe
        self._wake_confirm_timeout_s = max(0.5, float(wake_confirm_timeout_s))
        self._wake_poll_interval_s = max(0.1, float(wake_poll_interval_s))

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.state["devices"] = {ip: asdict(d) for ip, d in self.devices.items()}
        self.state["scanned_at"] = self.scanned_at
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._file)

    async def _vendor(self, client: httpx.AsyncClient, mac: str) -> str:
        if not mac or private_mac(mac):
            return ""
        prefix = mac[:8]
        cache = self.state.setdefault("vendors", {})
        if prefix in OUI:
            return OUI[prefix]
        if prefix in cache:
            return cache[prefix]
        try:
            response = await client.get(f"https://api.macvendors.com/{prefix}", timeout=4.0)
            vendor = response.text.strip()[:40] if response.status_code == 200 else ""
        except httpx.HTTPError:
            vendor = ""
        cache[prefix] = vendor
        await asyncio.sleep(1.1)  # o serviço aceita 1 pergunta por segundo
        return vendor

    async def scan(self) -> list[Device]:
        async with self._scan_lock:
            nets = local_networks()
            await asyncio.gather(*(ping_sweep(net) for net in nets))
            arp, ssdp, mdns = await asyncio.gather(asyncio.to_thread(arp_table), asyncio.to_thread(ssdp_search), asyncio.to_thread(mdns_browse))
            ips = {ip for ip in arp if any(ipaddress.ip_address(ip) in net for net in nets)} | set(ssdp) | set(mdns)
            found: dict[str, Device] = {}
            async with httpx.AsyncClient(headers={"User-Agent": "Kira/8"}) as client:
                infos = await asyncio.gather(*(upnp_info(client, url) for url in ssdp.values()))
                upnp = dict(zip(ssdp.keys(), infos))
                for ip in sorted(ips, key=lambda value: tuple(int(p) for p in value.split("."))):
                    if ip.endswith(".255") or ip.startswith("224.") or ip.startswith("239."):
                        continue
                    old = self.devices.get(ip)
                    device = Device(ip=ip, mac=arp.get(ip, ""), nickname=old.nickname if old else "")
                    info = upnp.get(ip) or {}
                    device.name = info.get("name") or (mdns.get(ip) or {}).get("name") or ""
                    device.maker = info.get("maker") or (mdns.get(ip) or {}).get("props", {}).get("manufacturer", "")
                    device.model = info.get("model") or (mdns.get(ip) or {}).get("props", {}).get("md", "")
                    device.services = sorted(set((mdns.get(ip) or {}).get("services", []) + (["UPnP"] if ip in upnp else [])))
                    if not device.maker:
                        device.maker = await self._vendor(client, device.mac)
                    if not device.name:
                        try:
                            device.name = (await asyncio.to_thread(socket.gethostbyaddr, ip))[0].split(".")[0]
                        except OSError:
                            pass
                    classify(device)
                    found[ip] = device
            for ip, old in self.devices.items():  # adicionados à mão (e nomes dados) ficam mesmo sem aparecer na busca
                if ip not in found and (old.kind == "adicionado à mão" or old.nickname):
                    found[ip] = old
            self.devices = found
            self.scanned_at = time.time()
            self._save()
            logger.info("SMART_HOME_SCAN devices=%d controllable=%d", len(found), sum(1 for d in found.values() if d.control))
            return list(found.values())

    def mine(self) -> list[Device]:
        """Os aparelhos que o Chefe adicionou (deu nome ou adicionou à mão): é o que aparece no painel e o que a Kira usa."""
        return [device for device in self.devices.values() if device.nickname or device.kind == "adicionado à mão"]

    def find(self, wanted: str) -> Device | None:
        """Pelo IP, pelo nome dado, pelo tipo ("tv", "televisão", "chromecast") ou, sem nome nenhum, o único aparelho que
        a Kira controla. Os aparelhos do Chefe vêm antes dos só vistos na rede ("coloca na TV" = a TV da sala dele)."""
        key = " ".join(str(wanted or "").strip().lower().split())
        if key in self.devices:
            return self.devices[key]
        aliases = {"televisao": "tv", "televisão": "tv", "a tv": "tv", "tv da sala": "tv"}
        kind_key = aliases.get(key, key)
        ordered = sorted(self.devices.values(), key=lambda d: (not (d.nickname or d.kind == "adicionado à mão"), not d.control))
        generic_tv_request = kind_key == "tv" and key in {"tv", "televisao", "televisão", "a tv", "tv da sala"}
        if not generic_tv_request:
            for device in ordered:
                label = device.label.lower()
                if key and (key in label or label in key or key == device.kind.lower() or key in device.maker.lower()):
                    return device
        if kind_key in {"tv", ""}:
            controllable = [d for d in ordered if d.control]
            tvs = [d for d in controllable if "tv" in d.kind.lower()] or controllable
            if tvs:
                if len(tvs) > 1:
                    named = [d for d in tvs if d.nickname]
                    if named:
                        return named[0]
                    logger.warning("SMART_HOME_DEVICE_AMBIGUOUS kind=tv candidates=%d", len(tvs))
                    return None
                return tvs[0]
        return None

    def add(self, ip: str, nickname: str = "") -> Device:
        device = self.devices.get(ip) or Device(ip=ip, kind="adicionado à mão")
        device.nickname = " ".join(str(nickname or "").split())[:40] or device.nickname
        self.devices[ip] = device
        self._save()
        return device

    def rename(self, ip: str, nickname: str) -> bool:
        device = self.devices.get(ip)
        if device is None:
            return False
        device.nickname = " ".join(str(nickname or "").split())[:40]
        self._save()
        return True

    # ---------- controle ----------
    async def command(self, device: Device, action: str, value: Any = None) -> dict[str, Any]:
        started = time.perf_counter()
        result: dict[str, Any] | None = None
        normalized_action = str(action or "").strip().lower()
        try:
            if device.control == "chromecast":
                result = await asyncio.to_thread(self._cast, device, normalized_action, value)
            elif device.control == "lgtv":
                media = self.__dict__.setdefault("_tv_media_at", {})
                if normalized_action == "abaixar_para_ouvir" and time.time() - media.get(device.ip, 0.0) > 4 * 3600:
                    result = {"ok": True, "confirmed": False, "feito": normalized_action, "motivo": "sem mídia tocando"}
                    return result  # TV sem mídia pedida pela Kira: nem conecta (TV desligada = 25 s de espera à toa)
                result = await self._lg(device, normalized_action, value)
                if result.get("ok") and normalized_action in {"youtube", "abrir", "tarefa", "tocar"}:
                    media[device.ip] = time.time()
                elif result.get("ok") and normalized_action in {"pausar", "desligar", "parar"}:
                    media.pop(device.ip, None)
            else:
                result = {
                    "ok": False,
                    "confirmed": False,
                    "message": f"{device.label} aparece na rede, mas a Kira ainda não sabe controlar esse aparelho.",
                }
            return result
        finally:
            logger.info(
                "SMART_HOME_COMMAND action=%s device_ref=%s control=%s ok=%s confirmed=%s duration_ms=%.0f",
                normalized_action or "unknown",
                self._device_ref(device),
                device.control or "none",
                str(bool(result and result.get("ok"))).lower(),
                str(bool(result and result.get("confirmed"))).lower(),
                (time.perf_counter() - started) * 1000.0,
            )

    @staticmethod
    def _device_ref(device: Device) -> str:
        raw = "|".join((str(device.ip), str(device.mac), str(device.model), str(device.control)))
        return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]

    @staticmethod
    def _cast(device: Device, action: str, value: Any) -> dict[str, Any]:
        import pychromecast

        cast = pychromecast.get_chromecast_from_host((device.ip, 8009, None, device.name, None))
        cast.wait(timeout=8)
        media = cast.media_controller
        if action == "pausar":
            media.pause()
        elif action in {"tocar", "continuar"}:
            media.play()
        elif action == "parar":
            cast.quit_app()
        elif action == "volume":
            cast.set_volume(max(0.0, min(1.0, float(value or 30) / 100)))
        elif action == "mudo":
            cast.set_volume_muted(True)
        else:
            return {"ok": False, "message": "No Chromecast: pausar, tocar, parar, volume (0-100) ou mudo."}
        return {"ok": True, "confirmed": True, "feito": action, "aparelho": device.label}

    @staticmethod
    def _wake(mac: str, ip: str = "") -> bool:
        """Liga a TV desligada (Wake-on-LAN): pacote mágico pela rede de casa. Precisa do "Ligar pela rede" ativo na TV."""
        import socket

        digits = "".join(ch for ch in str(mac or "") if ch in "0123456789abcdefABCDEF")
        if len(digits) != 12:
            return False
        packet = b"\xff" * 6 + bytes.fromhex(digits) * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # Com várias placas (Tailscale, VPN) o 255.255.255.255 sai pela errada: manda também pela rede da TV.
            targets = ["255.255.255.255"]
            if ip.count(".") == 3:
                targets += [ip.rsplit(".", 1)[0] + ".255", ip]
            for _ in range(3):
                for target in targets:
                    for port in (9, 7):
                        try:
                            sock.sendto(packet, (target, port))
                        except OSError:
                            pass
        return True

    @staticmethod
    async def _learn_pause(client: Any, app_id: str) -> None:
        """1ª pausa num app: confere se a imagem parou. Se ainda anda, tenta OK, OK (no Prime IPTV o 1º OK só mostra a
        barra e o 2º pausa) e guarda o que funcionou; da próxima vez vai direto (2026-11-07)."""
        from services.tv_agent import _still
        try:
            if await _still(client):
                tv_learn(app_id, pausar=["PAUSE"], tocar=["PLAY"])
                return
            for button in ("ENTER", "ENTER"):
                await client.button(button)
                await asyncio.sleep(0.6)
            if await _still(client):
                tv_learn(app_id, pausar=["ENTER", "ENTER"], tocar=["ENTER"])
        except Exception as exc:  # noqa: BLE001 - sem foto da TV: só não aprende
            logger.info("TV_LEARN_PAUSE_SKIPPED app=%s reason=%s", app_id, type(exc).__name__)

    @staticmethod
    async def _first_video(query: str) -> dict[str, Any] | None:
        """Primeiro vídeo da busca de música da própria Kira (mesma chave do YouTube)."""
        try:
            async with httpx.AsyncClient(timeout=12) as http:
                found = (await http.get("http://127.0.0.1:3000/api/music/search", params={"q": query, "max_results": 3})).json()
            return next((v for v in (found.get("data") or {}).get("results") or [] if v.get("videoId")), None)
        except Exception:
            return None

    async def _lg(self, device: Device, action: str, value: Any) -> dict[str, Any]:
        if action == "ligar":
            if self._wake_sender is None:
                sent = await asyncio.to_thread(self._wake, device.mac, device.ip)
            else:
                sent = self._wake_sender(device)
                if inspect.isawaitable(sent):
                    sent = await sent
                sent = bool(sent)
            if not sent:
                logger.info("SMART_HOME_WAKE_SENT device_ref=%s sent=false", self._device_ref(device))
                return {
                    "ok": False,
                    "confirmed": False,
                    "feito": "ligar",
                    "aparelho": device.label,
                    "message": "Não sei o endereço físico (MAC) da TV para ligar pela rede.",
                }
            confirmed, attempts = await self._wait_for_lg_after_wake(device)
            logger.info(
                "SMART_HOME_WAKE_CONFIRMED device_ref=%s confirmed=%s attempts=%d",
                self._device_ref(device),
                str(confirmed).lower(),
                attempts,
            )
            if not confirmed:
                return {
                    "ok": False,
                    "confirmed": False,
                    "feito": "ligar",
                    "aparelho": device.label,
                    "message": "Enviei o pedido para ligar, mas não consegui confirmar que a TV acordou. Não diga que ligou.",
                }
            return {
                "ok": True,
                "confirmed": True,
                "feito": "ligar",
                "aparelho": device.label,
                "nota": "TV acordada e acessível pela rede.",
            }

        from aiowebostv import WebOsClient
        keys = self.state.setdefault("lg_keys", {})
        client = WebOsClient(device.ip, keys.get(device.ip))
        try:
            try:
                await asyncio.wait_for(client.connect(), 25)
            except Exception:  # logo depois de outro comando a TV às vezes recusa: mais uma vez
                await asyncio.sleep(2)
                client = WebOsClient(device.ip, keys.get(device.ip))
                await asyncio.wait_for(client.connect(), 25)
        except Exception as exc:  # noqa: BLE001 - TV desligada ou esperando aceitar na tela
            from services.channel_logs import tv_log
            tv_log("TV_CONEXAO_RECUSADA", level=30, tv=device.ip, acao=action, erro=type(exc).__name__)
            return {"ok": False, "message": f"A TV não aceitou a conexão ({type(exc).__name__}). Se apareceu um pedido na tela da TV, aceite e peça de novo."}
        if client.client_key and keys.get(device.ip) != client.client_key:
            keys[device.ip] = client.client_key
            self._save()
        try:
            if action == "desligar":
                await client.power_off()
            elif action == "volume":
                await client.set_volume(int(max(0, min(100, float(value or 15)))))
            elif action == "abaixar_para_ouvir":  # Chefe chamou a Kira com a TV alta: abaixa até ela responder
                current = int(await client.get_volume() or 0)
                ducked = self.__dict__.setdefault("_tv_ducked", {})
                if current <= 12 or device.ip in ducked:
                    return {"ok": True, "confirmed": False, "feito": action, "volume": current}
                ducked[device.ip] = current
                await client.set_volume(max(6, current // 3))
                value = current
            elif action == "restaurar_volume":
                before = self.__dict__.setdefault("_tv_ducked", {}).pop(device.ip, None)
                if before is None:
                    return {"ok": True, "confirmed": False, "feito": action}
                current = int(await client.get_volume() or 0)
                if current <= max(6, before // 3):  # se o Chefe mexeu no volume nesse meio-tempo, fica o dele
                    await client.set_volume(int(before))
                value = before
            elif action == "mudo":
                await client.set_mute(True)
            elif action == "aumentar":
                await client.volume_up()
            elif action == "diminuir":
                await client.volume_down()
            elif action in {"desmutar", "tirar do mudo"}:
                await client.set_mute(False)
            elif action == "youtube":
                # "coloca tal música na TV": acha o vídeo exato (busca da Kira) e manda o YouTube da TV abrir ELE. YouTube
                # fechado abre na tela de escolher conta (o Chefe tem 3): OK na conta marcada e só então o vídeo.
                query = " ".join(str(value or "").split())[:120]
                video = await self._first_video(query) if query else None
                if str(await client.get_current_app() or "") != "youtube.leanback.v4":
                    await client.launch_app("youtube.leanback.v4")
                    await asyncio.sleep(8)
                    await client.button("ENTER")
                    await asyncio.sleep(4)
                if video:
                    await client.launch_app_with_params("youtube.leanback.v4", {"contentTarget": f"v={video['videoId']}"})
                    value = video.get("title") or query
                elif query:
                    await client.launch_app_with_params("youtube.leanback.v4", {"contentTarget": f"https://www.youtube.com/results?search_query={query}"})
                    value = f"busca: {query}"
            elif action == "abrir":
                # nome tolerante: sem acento/espaço ("prime iptv", "primevideo"); igual > começa com > contém
                apps = [app for app in (await client.get_apps_all()) or [] if app.get("visible", True) and app.get("title")]
                key = lambda text: re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", str(text).lower()).encode("ascii", "ignore").decode())
                wanted = key(value)
                found = next((app for test in (lambda t: t == wanted, lambda t: t.startswith(wanted), lambda t: wanted in t)
                              for app in apps if wanted and test(key(app["title"]))), None)
                if found is None:
                    return {"ok": False, "message": f"Não achei '{value}' na TV.", "apps": sorted({str(app["title"]) for app in apps})}
                await client.launch_app(found["id"])
                value = found.get("title")
            elif action == "apps":
                apps = (await client.get_apps_all()) or []
                return {
                    "ok": True,
                    "confirmed": True,
                    "apps": sorted({str(app["title"]) for app in apps if app.get("visible", True) and app.get("title")}),
                }
            elif action == "tarefa":  # "coloca tal série no Prime IPTV": olha a tela e aperta os botões até chegar
                from services.tv_agent import run
                try:  # termina antes do limite de 150 s da ferramenta: cancelar para de apertar botões na TV
                    result = await asyncio.wait_for(run(client, str(value or "")), timeout=135)
                except asyncio.TimeoutError:
                    return {"ok": False, "confirmed": False, "aparelho": device.label,
                            "fala": "Demorou demais navegando na TV; parei para não ficar apertando botões. Nada foi confirmado."}
                # tv_agent só devolve ok=True depois de conferir a tela da TV (vídeo andando)
                return {**result, "confirmed": bool(result.get("ok")), "aparelho": device.label}
            elif action == "tela_kira":  # página da Kira no navegador da TV; guarda o app de antes para restaurar
                from services.tv_screen import SCREEN, TEMAS, lan_ip
                tema = str(value or "orbe").strip().lower()
                tema = tema if tema in TEMAS else "orbe"
                prev = self.state.setdefault("tv_tela_prev", {})
                current = str(await client.get_current_app() or "")
                if device.ip not in prev and current != "com.webos.app.browser":
                    prev[device.ip] = current or "com.webos.app.livetv"
                    self._save()
                host = await asyncio.to_thread(lan_ip, device.ip)
                port = await asyncio.to_thread(SCREEN.start, host)
                if current == "com.webos.app.browser":  # navegador já aberto ignora o endereço novo: fecha e abre de novo
                    try:
                        await client.close_app("com.webos.app.browser")
                        await asyncio.sleep(1.5)
                    except Exception:  # noqa: BLE001
                        pass
                await client.launch_app_with_params("com.webos.app.browser", {"target": f"http://{host}:{port}/?tema={tema}"})
                from services.channel_logs import tv_log
                tv_log("TV_TELA_KIRA_ABERTA", tv=device.ip, antes=prev.get(device.ip, ""), url=f"http://{host}:{port}/")
                value = tema
            elif action == "restaurar_tela":  # volta ao que estava antes, como se nada tivesse acontecido
                from services.tv_screen import SCREEN
                before = self.state.setdefault("tv_tela_prev", {}).pop(device.ip, "") or "com.webos.app.livetv"
                self._save()
                try:
                    await client.close_app("com.webos.app.browser")
                except Exception:  # noqa: BLE001 - navegador já fechado
                    pass
                await client.launch_app(before)
                await asyncio.to_thread(SCREEN.stop)
                from services.channel_logs import tv_log
                tv_log("TV_RESTAURADA", tv=device.ip, voltou_para=before)
                value = before
            elif action == "print_tela":  # foto do que a TV mostra agora ("tira um print da TV"); valor = pasta
                from datetime import datetime
                from services.tv_agent import _shot
                data = await _shot(client)
                if not data:
                    return {"ok": False, "confirmed": False, "message": "A TV não entregou a foto da tela."}
                folder = Path(str(value)) if value and Path(str(value)).is_absolute() else Path.home() / "Documents" / "Kira" / "Imagens"
                folder.mkdir(parents=True, exist_ok=True)
                shot = folder / f"print_tv_{datetime.now():%Y-%m-%d_%H-%M-%S}.png"
                shot.write_bytes(data)
                return {"ok": True, "confirmed": True, "arquivo": str(shot), "aparelho": device.label}
            elif action == "tv":  # entrada da TV ao vivo (antena/cabo)
                await client.launch_app("com.webos.app.livetv")
            elif action == "canal":  # número do canal: aperta os dígitos e confirma
                for digit in re.sub(r"\D", "", str(value or ""))[:4]:
                    await client.button(digit)
                await client.button("ENTER")
            elif action in TV_BUTTONS:
                await client.button(TV_BUTTONS[action])
            elif action == "o que esta passando":
                current = await client.get_current_app()
                return {"ok": True, "confirmed": True, "app_aberto": str(current or "")}
            elif action in {"pausar", "tocar"}:
                app_id = str(await client.get_current_app() or "")
                learned = tv_learned(app_id, action)
                if learned:  # este app já ensinou o jeito dele (configs/tv_learned.json): direto, sem conferir
                    for button in learned:
                        await client.button(button)
                        await asyncio.sleep(0.35)
                else:
                    await (client.pause() if action == "pausar" else client.play())
                    # apps como o Prime IPTV ignoram o comando de mídia (2026-11-07: "pausado" e a série seguiu);
                    # o botão do controle remoto eles atendem
                    try:
                        await client.button("PAUSE" if action == "pausar" else "PLAY")
                    except Exception:  # noqa: BLE001 - TV sem o socket do controle: fica o comando de mídia
                        pass
                    if action == "pausar" and app_id:
                        await self._learn_pause(client, app_id)
            else:
                return {"ok": False, "message": "Na TV LG: ligar, desligar, volume (0-100), aumentar, diminuir, mudo, desmutar, youtube (valor = o que buscar), abrir (valor = app), apps, tv, canal (valor = número), canal+, canal-, lista, info, menu, sair, 0-9, o que esta passando, pausar, tocar, voltar, home, ok, setas."}
        finally:
            await client.disconnect()
        return {
            "ok": True,
            "confirmed": True,
            "feito": action,
            "aparelho": device.label,
            **({"valor": value} if value else {}),
        }

    async def _wait_for_lg_after_wake(self, device: Device) -> tuple[bool, int]:
        """Aguarda evidência de que o Wake-on-LAN acordou a TV, sem prometer pelo envio do pacote."""
        deadline = asyncio.get_running_loop().time() + self._wake_confirm_timeout_s
        attempts = 0
        while True:
            attempts += 1
            if self._wake_probe is None:
                reachable = await self._probe_lg_tcp(device)
            else:
                reachable = self._wake_probe(device)
                if inspect.isawaitable(reachable):
                    reachable = await reachable
                reachable = bool(reachable)
            if reachable:
                return True, attempts
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False, attempts
            await asyncio.sleep(min(self._wake_poll_interval_s, remaining))

    @staticmethod
    async def _probe_lg_tcp(device: Device) -> bool:
        """Probe leve de webOS; usado apenas depois de um comando explícito de ligar."""
        for port in (3000, 3001):
            writer = None
            try:
                _reader, writer = await asyncio.wait_for(asyncio.open_connection(device.ip, port), timeout=1.0)
                return True
            except (OSError, asyncio.TimeoutError):
                continue
            finally:
                if writer is not None:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
        return False


__all__ = ["Device", "SmartHome", "classify", "private_mac", "arp_table", "ssdp_search", "mdns_browse"]
