"""
KIRA - Instalar no Startup do Windows
======================================
Rode este arquivo UMA VEZ para que a Kira inicie automaticamente com o Windows.
Para remover, rode novamente e escolha a opção de desinstalar.
"""

import sys
import winreg
from pathlib import Path

# ── Configuração ──────────────────────────────
APP_NAME = "KiraAssistente"
# 24/7 (2026-11-07): abre pelo Kira.bat (limpeza, venv certo, reabre em queda) com --autostart: a sessão inicia sozinha.
# Antes apontava para pythonw + kira_app.py direto (e para uma pasta antiga), sem reabertura nem sessão automática.
BAT = str(Path(__file__).resolve().parent.parent / "Kira.bat")
CMD = f'cmd /c start "Kira" /min "{BAT}" --autostart'
# ──────────────────────────────────────────────

REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def esta_instalado():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False


def instalar():
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, CMD)
    winreg.CloseKey(key)
    print(f"\nKira registrada no startup!\n")
    print(f"   Comando: {CMD}")
    print(f"\n   Na próxima vez que ligar o Windows, a Kira vai iniciar sozinha.")


def desinstalar():
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE)
    winreg.DeleteValue(key, APP_NAME)
    winreg.CloseKey(key)
    print("\nKira removida do startup.")


def main():
    if "--instalar" in sys.argv:  # sem perguntas (usado pela Kira/Codex)
        instalar()
        return
    print("=" * 50)
    print("  KIRA - Configurar inicialização automática")
    print("=" * 50)

    if esta_instalado():
        print("\nKira JÁ está configurada para iniciar com o Windows.")
        print("\nO que deseja fazer?")
        print("  1 - Manter (não fazer nada)")
        print("  2 - Remover do startup")
        escolha = input("\nEscolha (1 ou 2): ").strip()
        if escolha == "2":
            desinstalar()
        else:
            print("\nNada alterado.")
    else:
        print("\nKira ainda NÃO inicia com o Windows.")
        print("\nO que deseja fazer?")
        print("  1 - Instalar (iniciar com o Windows)")
        print("  2 - Cancelar")
        escolha = input("\nEscolha (1 ou 2): ").strip()
        if escolha == "1":
            instalar()
        else:
            print("\nCancelado.")

    input("\nPressione Enter para fechar...")


if __name__ == "__main__":
    main()
