"""
Gerenciador centralizado de processos da Kira.
Registra todo processo iniciado e garante cleanup completo.
"""
import atexit
import json
import logging
import os
import signal
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import psutil

PROCESS_REGISTRY_FILE = Path(__file__).parent.parent.parent / "runtime" / "kira_processes.json"
LOCK_FILE = Path(__file__).parent.parent.parent / "runtime" / "kira.lock"
ENTRYPOINT_LOCK_FILE = Path(__file__).parent.parent.parent / "runtime" / "entrypoint.lock"


def _is_kira_process(pid: int, lock_file: Path) -> bool:
    """O PID da trava ainda é a Kira? Depois de a Kira ser fechada à força a trava fica, e o Windows reaproveita o
    número para outro processo (2026-10-26: um svchost com o PID antigo travou toda sessão em "iniciando"). Vale só um
    Python da Kira que já existia quando a trava foi gravada."""
    try:
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline()).lower()
        if "python" not in proc.name().lower() or not ("kira" in cmdline or "agent.py" in cmdline):
            return False
        return proc.create_time() <= lock_file.stat().st_mtime + 2.0
    except (psutil.Error, OSError):
        return False


@dataclass
class ProcessRecord:
    pid: int
    name: str
    command: str
    started_at: str
    parent_pid: Optional[int] = None


class ProcessTracker:
    def __init__(self):
        self.logger = logging.getLogger("kira.process_tracker")
        self.processes: Dict[int, ProcessRecord] = {}
        self._load_registry()
        atexit.register(self.cleanup_all)
        self._install_emergency_cleanup_handlers()

    def _install_emergency_cleanup_handlers(self) -> None:
        """Garante remocao de locks tambem em encerramentos por sinal."""
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            if sig is None:
                continue
            try:
                previous_handler = signal.getsignal(sig)

                def _handler(signum, frame, *, _previous_handler=previous_handler):
                    self.logger.warning("[PROCESS] emergency_cleanup_signal signum=%s", signum)
                    self.release_locks_for_current_process()
                    if callable(_previous_handler):
                        _previous_handler(signum, frame)
                    elif _previous_handler == signal.SIG_DFL:
                        raise SystemExit(128 + int(signum))

                signal.signal(sig, _handler)
            except Exception as exc:
                self.logger.debug("[PROCESS] signal_cleanup_install_failed signal=%s reason=%s", sig, exc)

    def _load_registry(self):
        """Carrega processos registrados do arquivo"""
        if PROCESS_REGISTRY_FILE.exists():
            try:
                with open(PROCESS_REGISTRY_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                    for record in data:
                        self.processes[record["pid"]] = ProcessRecord(**record)
            except Exception as e:
                self.logger.warning("[PROCESS] Falha ao carregar registry: %s", e)

    def _save_registry(self):
        """Salva processos registrados no arquivo"""
        PROCESS_REGISTRY_FILE.parent.mkdir(exist_ok=True)
        with open(PROCESS_REGISTRY_FILE, "w", encoding="utf-8") as f:
            data = [asdict(p) for p in self.processes.values()]
            json.dump(data, f, indent=2)

    def check_already_running(self) -> bool:
        """Verifica se ja existe uma instancia da Kira rodando"""
        if LOCK_FILE.exists():
            try:
                with open(LOCK_FILE, encoding="utf-8") as f:
                    lock_pid = int(f.read().strip())
                if _is_kira_process(lock_pid, LOCK_FILE):
                    self.logger.error("[PROCESS] Kira ja esta rodando (PID %s)", lock_pid)
                    return True
                else:
                    LOCK_FILE.unlink(missing_ok=True)
            except Exception as e:
                self.logger.warning("[PROCESS] Lock file invalido, recreando: %s", e)
                LOCK_FILE.unlink(missing_ok=True)

        LOCK_FILE.parent.mkdir(exist_ok=True)
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        self.logger.info("[PROCESS] lock acquired pid=%s", os.getpid())
        return False

    def entrypoint_blocker(self) -> Optional[int]:
        """Antes de dizer "pronto": PID de outra Kira que atenderia as sessões, ou None. Trava abandonada (Kira fechada
        à força, PID reaproveitado pelo Windows) é apagada aqui, para a primeira sessão não ser recusada."""
        try:
            pid = int(ENTRYPOINT_LOCK_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            ENTRYPOINT_LOCK_FILE.unlink(missing_ok=True)
            return None
        if pid != os.getpid() and _is_kira_process(pid, ENTRYPOINT_LOCK_FILE):
            return pid
        if pid != os.getpid():
            self.logger.warning("[ENTRYPOINT] lock stale no registro do worker (PID %s) - removendo", pid)
            ENTRYPOINT_LOCK_FILE.unlink(missing_ok=True)
        return None

    def ensure_single_entrypoint(self) -> None:
        """Garante que apenas um entrypoint LiveKit esteja ativo por vez."""
        ENTRYPOINT_LOCK_FILE.parent.mkdir(exist_ok=True)
        if ENTRYPOINT_LOCK_FILE.exists():
            try:
                existing_pid = int(ENTRYPOINT_LOCK_FILE.read_text(encoding="utf-8").strip())
            except Exception as exc:
                self.logger.warning("[ENTRYPOINT] lock invalido, recriando: %s", exc)
                ENTRYPOINT_LOCK_FILE.unlink(missing_ok=True)
            else:
                if existing_pid == os.getpid():
                    self.logger.info("[ENTRYPOINT] lock ja pertence ao PID atual=%s", existing_pid)
                    return
                if _is_kira_process(existing_pid, ENTRYPOINT_LOCK_FILE):
                    raise RuntimeError(f"[ENTRYPOINT] Ja existe entrypoint ativo (PID {existing_pid})")
                self.logger.warning(
                    "[ENTRYPOINT] lock stale encontrado (PID %s) - removendo",
                    existing_pid,
                )
                ENTRYPOINT_LOCK_FILE.unlink(missing_ok=True)

        ENTRYPOINT_LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        self.logger.info("[ENTRYPOINT] lock criado para PID %s", os.getpid())

    def register_process(self, proc: psutil.Popen, name: str) -> int:
        """Registra processo iniciado pela Kira"""
        args = proc.args if hasattr(proc, "args") else []
        if isinstance(args, (list, tuple)):
            command = " ".join(str(x) for x in args)
        else:
            command = str(args)

        record = ProcessRecord(
            pid=proc.pid,
            name=name,
            command=command,
            started_at=datetime.now().isoformat(),
            parent_pid=os.getpid(),
        )
        self.processes[proc.pid] = record
        self._save_registry()
        self.logger.info("[PROCESS] registered name=%s pid=%s", name, proc.pid)
        return proc.pid

    def start_and_register(self, name: str, args: List[str], cwd: Optional[str] = None, **kwargs):
        """Inicia processo E registra"""
        import subprocess

        proc = subprocess.Popen(args, cwd=cwd, **kwargs)
        self.register_process(proc, name)
        return proc

    def is_kira_process(self, pid: int) -> bool:
        """Verifica se PID foi iniciado pela Kira"""
        return pid in self.processes

    def cleanup_all(self):
        """Encerra processos registrados iniciados por este processo"""
        self.logger.info("[CLEANUP] Iniciando limpeza de processos")

        owned = [
            pid for pid, record in self.processes.items() if (record.parent_pid is None or record.parent_pid == os.getpid())
        ]
        for pid in owned:
            self._kill_process_tree(pid)

        self._save_registry()
        self._remove_lock_if_owner()
        self.release_entrypoint_lock()

        # remove registro vazio
        if not self.processes and PROCESS_REGISTRY_FILE.exists():
            PROCESS_REGISTRY_FILE.unlink(missing_ok=True)

        self.logger.info("[CLEANUP] Limpeza concluida")

    def _remove_lock_if_owner(self) -> None:
        if not LOCK_FILE.exists():
            return
        try:
            lock_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            if lock_pid == os.getpid() or not psutil.pid_exists(lock_pid):
                LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            LOCK_FILE.unlink(missing_ok=True)

    def _remove_entrypoint_lock_if_owner(self) -> None:
        if not ENTRYPOINT_LOCK_FILE.exists():
            return
        try:
            lock_pid = int(ENTRYPOINT_LOCK_FILE.read_text(encoding="utf-8").strip())
            if lock_pid == os.getpid() or not psutil.pid_exists(lock_pid):
                ENTRYPOINT_LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            ENTRYPOINT_LOCK_FILE.unlink(missing_ok=True)

    def release_entrypoint_lock(self) -> None:
        self._remove_entrypoint_lock_if_owner()

    def release_locks_for_current_process(self) -> None:
        self._remove_lock_if_owner()
        self._remove_entrypoint_lock_if_owner()

    def _kill_process_tree(self, pid: int):
        """Mata processo E seus filhos recursivamente"""
        record = self.processes.get(pid)
        if not record:
            return

        try:
            if not psutil.pid_exists(pid):
                self.logger.info("[CLEANUP] pid=%s name=%s ja encerrado", pid, record.name)
                del self.processes[pid]
                return

            proc = psutil.Process(pid)

            children = proc.children(recursive=True)
            for child in children:
                try:
                    self.logger.info("[CLEANUP] killing child pid=%s parent=%s", child.pid, pid)
                    child.terminate()
                except Exception as e:
                    self.logger.warning("[CLEANUP] Erro ao matar filho %s: %s", child.pid, e)

            gone, alive = psutil.wait_procs(children, timeout=3)
            _ = gone
            for p in alive:
                try:
                    p.kill()
                except Exception:
                    pass

            self.logger.info("[CLEANUP] killing pid=%s name=%s", pid, record.name)
            proc.terminate()
            proc.wait(timeout=5)

        except psutil.NoSuchProcess:
            self.logger.info("[CLEANUP] pid=%s name=%s ja nao existe", pid, record.name)
        except psutil.TimeoutExpired:
            self.logger.warning("[CLEANUP] pid=%s nao respondeu a terminate, usando kill", pid)
            try:
                proc.kill()
            except Exception:
                pass
        except Exception as e:
            self.logger.error("[CLEANUP] erro ao matar pid=%s: %s", pid, e)
        finally:
            if pid in self.processes:
                del self.processes[pid]


_tracker = None


def get_tracker() -> ProcessTracker:
    global _tracker
    if _tracker is None:
        _tracker = ProcessTracker()
    return _tracker
