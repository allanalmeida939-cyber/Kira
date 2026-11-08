"""Pacote do agente Kira.

Mantem compatibilidade entre imports via ``agent.*`` e execucao direta dentro da
pasta ``agent/`` adicionando a raiz do projeto e a propria pasta do pacote ao
``sys.path`` quando necessario.
"""

from __future__ import annotations

import sys
from pathlib import Path


_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_DIR.parent

for _candidate in (str(_PACKAGE_DIR), str(_PROJECT_ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)
