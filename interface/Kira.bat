@echo off
title Kira
cd /d "%~dp0\.."

if not exist "Kira.bat" (
    echo ERRO: nao encontrei o launcher principal Kira.bat em:
    echo %cd%
    pause
    exit /b 1
)

call "Kira.bat"
