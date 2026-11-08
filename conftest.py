"""Testes nunca gravam na memória real do Chefe nem falam na tela dele.

Antes do primeiro teste, o registro da última conversa (configs/session_recap.json), o histórico do dia
(memory/local/conversas) e os interesses (configs/interests.json) passam a existir só na memória do processo de teste,
e a fila de transcrição vai para uma pasta temporária: a Kira publica essa fila na conversa (a cada 0,5 s, ou ao ligar).
Sem isso, falas de teste iam parar no histórico de verdade e na tela (D105). Os testes do agente, rodados de dentro de
agent/, usam o agent/tests/conftest.py, que faz o mesmo.
"""

import importlib
import shutil
import tempfile
from pathlib import Path


def pytest_collection_finish(session):
    # Depois da coleta, para não mudar qual pacote "services" cada teste importou (o da raiz ou o agent/services).
    recap = importlib.import_module("services.session_recap")
    interests = importlib.import_module("services.interests")
    queue = importlib.import_module("services.transcription_queue")
    recap._SHARED = recap.SessionRecap(path=None)
    interests._SHARED = interests.Interests(None)
    folder = Path(tempfile.mkdtemp(prefix="kira-testes-"))
    session.config.add_cleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
    queue._queue = queue.TranscriptionQueue(str(folder / "transcription_queue.db"))
