@echo off
title KIRA I.A - Cyberpunk Boot
cd /d "%~dp0"
REM --autostart = aberta pelo Windows (ligou o PC / voltou a energia): a UI inicia a sessao sozinha.
set "KIRA_AUTO_START_SESSION="
if /i "%~1"=="--autostart" set "KIRA_AUTO_START_SESSION=1"
set "PROJECT_DIR=%cd%"
set "PREBOOT_SCRIPT=%PROJECT_DIR%\scripts\preboot_cleanup_kira.ps1"
set "OFFICIAL_PYTHON_EXE=%PROJECT_DIR%\agent\venv\Scripts\python.exe"
set "LOG_ROOT=%PROJECT_DIR%\logs"
set "BACKEND_LOG_DIR=%LOG_ROOT%\Backend"
if not exist "%BACKEND_LOG_DIR%" mkdir "%BACKEND_LOG_DIR%"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH-mm-ss"') do set "KIRA_LOG_TIMESTAMP=%%i"
set "PREBOOT_LOG=%BACKEND_LOG_DIR%\preboot_%KIRA_LOG_TIMESTAMP%.log"
set "BACKEND_OUTPUT_LOG=%BACKEND_LOG_DIR%\backend_output_%KIRA_LOG_TIMESTAMP%.log"

echo.
echo        K I R A   I . A
echo        CYBERPUNK CORE // BOOT
echo.
echo        Preparando o nucleo da Kira...
echo.

echo [%TIME%] [1/7] Limpando processos antigos...
if not exist "%PREBOOT_SCRIPT%" (
    echo [ERRO] Modulo de inicializacao nao encontrado.
    echo        Detalhes: %PREBOOT_SCRIPT%
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PREBOOT_SCRIPT%" > "%PREBOOT_LOG%" 2>&1
if errorlevel 2 (
    echo [ERRO] Portas criticas continuam ocupadas por outra instancia.
    echo        Diagnostico: %PREBOOT_LOG%
    pause
    exit /b 2
)
if errorlevel 1 (
    echo [ERRO] A limpeza inicial falhou.
    echo        Diagnostico: %PREBOOT_LOG%
    pause
    exit /b 1
)
echo [%TIME%] [1/7] Limpeza concluida. OK

set "KIRA_VENV_PATH="
set "PYTHON_EXE="
set "VENV_LABEL="
call :TryPython "%OFFICIAL_PYTHON_EXE%" "venv" "%PROJECT_DIR%\agent\venv"

if not defined PYTHON_EXE (
    echo [ERRO] Ambiente Python oficial indisponivel.
    echo        Detalhes: %OFFICIAL_PYTHON_EXE%
    pause
    exit /b 1
)

echo [%TIME%] [2/7] Ambiente Python pronto. OK

if exist "%TEMP%\kira_bootstrap.lock" (
    del /f /q "%TEMP%\kira_bootstrap.lock" >nul 2>&1
)

set "PYTHONPATH=%PROJECT_DIR%;%PROJECT_DIR%\agent;%PROJECT_DIR%\whatsapp"
set "KIRA_PYTHON_EXE=%PYTHON_EXE%"
set "KIRA_VENV_LABEL=%VENV_LABEL%"
set "PYTHONNET_RUNTIME=coreclr"
set "PYWEBVIEW_GUI=qt"

REM [3/7] removido em 2026-11-02: importar clr/webview/livekit/fastapi/uvicorn/genai aqui custava 8,5 s e o backend
REM importa tudo de novo logo depois; a conferencia barata (find_spec) do passo 2 continua avisando se faltar algo.
echo [%TIME%] [3/7] Modulos conferidos no passo 2. OK

echo [%TIME%] [4/7] Preparando ambiente do backend...
echo [%TIME%] [5/7] Entregando o controle ao backend...
echo [%TIME%] [6/7] Iniciando a Kira; acompanhe as etapas no log do backend...
echo.
if exist "kira_backend_output.log" del "kira_backend_output.log" >nul 2>&1
if exist "kira_backend.log" del "kira_backend.log" >nul 2>&1
set "KIRA_CRASH_RESTARTS=0"
:RunKira
"%PYTHON_EXE%" -u kira_app.py >> "%BACKEND_OUTPUT_LOG%" 2>&1
set "KIRA_EXIT_CODE=%ERRORLEVEL%"
REM Codigo 0 = fechada de proposito (Sair na bandeja): nao reabre. Qualquer queda (ex. -1073741819 = 0xC0000005,
REM motor grafico Qt6WebEngineCore na GTX 550 Ti) reabre sozinha ate 5 vezes e ja inicia a sessao (24/7).
if not "%KIRA_EXIT_CODE%"=="0" if %KIRA_CRASH_RESTARTS% LSS 5 (
    set /a KIRA_CRASH_RESTARTS+=1
    set "KIRA_AUTO_START_SESSION=1"
    echo [%TIME%] Kira caiu (codigo %KIRA_EXIT_CODE%^). Reabrindo e iniciando a sessao...
    timeout /t 5 /nobreak >nul
    call :cleanup
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PREBOOT_SCRIPT%" >> "%PREBOOT_LOG%" 2>&1
    goto RunKira
)

echo [%TIME%] [7/7] Backend encerrado. Codigo: %KIRA_EXIT_CODE%

call :cleanup

echo.
echo KIRA I.A encerrada. Codigo: %KIRA_EXIT_CODE%
if %KIRA_EXIT_CODE% NEQ 0 (
    echo ERRO: verifique %BACKEND_OUTPUT_LOG%
)
if defined KIRA_AUTO_START_SESSION exit /b %KIRA_EXIT_CODE%
pause
exit /b %KIRA_EXIT_CODE%

:cleanup
echo [SHUTDOWN] Encerrando processos Kira...
taskkill /F /IM "livekit-server.exe" /T >nul 2>&1
taskkill /F /IM "QtWebEngineProcess.exe" /T >nul 2>&1
timeout /t 2 /nobreak >nul
echo [SHUTDOWN] Arvore encerrada.
exit /b 0

:TryPython
if defined PYTHON_EXE exit /b 0
if not exist "%~1" (
    exit /b 0
)
set "KIRA_PY_CHECK=%TEMP%\kira_python_check_%RANDOM%.txt"
"%~1" -c "import importlib.util,sys; mods=('clr','webview','livekit','fastapi','uvicorn','google.genai','noisereduce'); missing=[m for m in mods if importlib.util.find_spec(m) is None]; print('deps_ok' if not missing else 'missing=' + ','.join(missing)); sys.exit(1 if missing else 0)" >"%KIRA_PY_CHECK%" 2>&1
if errorlevel 1 (
    del "%KIRA_PY_CHECK%" >nul 2>&1
    exit /b 0
)
del "%KIRA_PY_CHECK%" >nul 2>&1
set "PYTHON_EXE=%~1"
set "VENV_LABEL=%~2"
set "KIRA_VENV_PATH=%~3"
exit /b 0
