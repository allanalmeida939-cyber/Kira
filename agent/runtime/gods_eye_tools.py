"""Tool do Olho de Deus (globo com terremotos, aviões e satélites). Abre só com a frase secreta do Chefe."""

from __future__ import annotations

import os

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local

SECRET_PHRASE = os.getenv("KIRA_GODS_EYE_PHRASE", "protocolo olho de Deus").strip() or "protocolo olho de Deus"
LAYERS = {"terremoto": "terremotos", "terremotos": "terremotos", "aviao": "avioes", "avioes": "avioes", "aviões": "avioes",
          "voos": "avioes", "satelite": "satelites", "satelites": "satelites", "satélites": "satelites", "navios": "navios",
          "starlink": "starlink", "lixo": "lixo", "detritos": "lixo", "destrocos": "lixo", "destroços": "lixo",
          "camera": "cameras", "cameras": "cameras", "câmera": "cameras", "câmeras": "cameras", "webcams": "cameras",
          "navio": "navios", "barcos": "navios", "chuva": "chuva", "radar": "chuva", "raios": "raios", "relampagos": "raios",
          "relâmpagos": "raios", "aurora": "aurora", "auroras": "aurora", "solar": "aurora", "sol": "aurora", "eventos": "eventos",
          "queimadas": "eventos", "incendios": "eventos", "incêndios": "eventos", "vulcoes": "eventos", "vulcões": "eventos",
          "tempestades": "eventos", "asteroides": "asteroides", "asteroide": "asteroides", "foto": "foto", "apod": "foto",
          "vento": "vento", "ventos": "vento", "nuvens": "vento", "mare": "mare", "maré": "mare", "mares": "mare", "marés": "mare",
          "bicicletas": "bicicletas", "bikes": "bicicletas", "bicicletar": "bicicletas", "radios": "radios", "rádios": "radios"}


def parse_layers(text: str) -> list[str]:
    found = []
    for word in str(text or "").lower().replace(",", " ").split():
        layer = LAYERS.get(word)
        if layer and layer not in found:
            found.append(layer)
    return found


class GodsEyeToolsMixin:
    @function_tool(
        name="olho_de_deus",
        description=(
            f"Modo secreto: a interface some e um globo mostra terremotos, aviões, satélites, Starlink, lixo espacial, navios, radar "
            f"de chuva, raios, auroras/tempestade solar (NOAA), queimadas e tempestades (NASA), asteroides passando perto e a "
            f"foto do dia da NASA, em tempo real. Abra quando o Chefe pedir o Olho de Deus de qualquer jeito ('ativa o olho de Deus', 'abre o globo', '"
            f"{SECRET_PHRASE}'). NUNCA diga que abriu, que o globo está girando ou cite números sem chamar esta ferramenta e "
            "receber a resposta dela. Com o globo aberto, use "
            "para 'mostra terremotos', 'aviões sobre Fortaleza', 'satélites', 'câmeras', 'tira os aviões', 'mostra as ruas "
            "do Benfica', 'mostra em 3D', 'volta pro globo', 'fecha'. acao: 'abrir', 'fechar', "
            "'focar' (ir para um lugar: a câmera viaja até lá; bairro, rua e endereço já descem para o mapa de "
            "satélite), 'camadas' (mostrar só as camadas pedidas), 'globo' (sair do mapa de perto e do 3D), '3d' "
            "(prédios em volume girando em volta do lugar) ou 'lugar' ('mostra essa casa', 'mostra o comércio X', "
            "'quero ver esse lugar de perto': cartão com foto de satélite, endereço, dados e fotos públicas). Não existe "
            "visão de rua: para 'anda na rua' use 'lugar'. camadas: palavras como 'terremotos avioes satelites "
            "cameras starlink lixo navios chuva raios aurora eventos asteroides foto vento mare bicicletas radios' (vento = vento, nuvens e temperatura do lugar; mare = maré das próximas horas; bicicletas = estações de bike compartilhada; radios = rádios perto; lixo = lixo espacial; aurora = clima "
            "espacial/tempestade solar; eventos = queimadas, tempestades e vulcões da NASA; foto = foto do dia da NASA); quando vierem, ficam SÓ essas, em qualquer ação ('mostra apenas aviões e foca em Fortaleza' = "
            "acao 'focar', camadas 'avioes', lugar 'Fortaleza'; faça tudo numa chamada só). lugar: cidade/bairro/rua/endereço com o nome oficial (vazio em '3d'/'lugar' = o ponto que está na tela). detalhe: 'cidade', 'bairro', 'ruas' "
            "ou 'casas' força o nível do mapa de satélite. Na resposta, fale os números que a ferramenta devolver "
            "(terremotos, satélites, aviões), do jeito que vieram."
        ),
    )
    async def olho_de_deus(self, acao: str, camadas: str = "", lugar: str = "", raio_km: float = 250.0, detalhe: str = "") -> str:
        payload = {"action": str(acao or "abrir").strip().lower(), "layers": parse_layers(camadas), "radius_km": raio_km,
                   "detail": str(detalhe or "").strip().lower()}
        if lugar.strip():
            payload["place"] = lugar.strip()
        try:
            result = await _post_local("/api/godseye/command", payload, read_timeout=25.0)
        except (httpx.HTTPError, TimeoutError):
            return "O Olho de Deus não respondeu agora."
        return str(result.get("summary") or "O Olho de Deus não respondeu agora.")


__all__ = ["GodsEyeToolsMixin", "parse_layers"]
