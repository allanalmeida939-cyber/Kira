@echo off
chcp 65001 >nul
title Kira - instalacao
cd /d "%~dp0"
echo.
echo   K I R A  -  instalacao
echo   (pode demorar de 10 a 30 minutos na primeira vez)
echo.

where py >nul 2>nul && (set "PY=py -3.12") || (set "PY=python")
%PY% --version >nul 2>nul || (echo [ERRO] Python 3.12 nao encontrado. Instale e marque "Add python.exe to PATH". & pause & exit /b 1)
where node >nul 2>nul || (echo [ERRO] Node.js nao encontrado. Instale o Node.js 20 LTS. & pause & exit /b 1)

echo [1/6] Ambiente Python (agent\venv)...
if not exist "agent\venv\Scripts\python.exe" %PY% -m venv agent\venv || (echo [ERRO] nao criou o venv & pause & exit /b 1)
set "VPY=%cd%\agent\venv\Scripts\python.exe"
"%VPY%" -m pip install --upgrade pip wheel setuptools

echo [2/6] Pacotes Python (o mais demorado)...
"%VPY%" -m pip install -r requirements-lock.txt
if errorlevel 1 (
  echo   Lista exata falhou; tentando a lista basica...
  "%VPY%" -m pip install -r requirements.txt || (echo [ERRO] pacotes Python & pause & exit /b 1)
)

echo [3/6] Navegador interno (Chromium)...
"%VPY%" -m playwright install chromium

echo [4/6] Ponte do WhatsApp (npm)...
pushd whatsapp_bridge && call npm install --omit=dev && popd

echo [5/6] FFmpeg e servidor de voz local (LiveKit)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol='Tls12';" ^
  "if (!(Test-Path 'bin\ffmpeg\ffmpeg.exe')) { New-Item -ItemType Directory -Force bin\ffmpeg | Out-Null; $z=\"$env:TEMP\ffmpeg.zip\"; Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $z; Expand-Archive $z \"$env:TEMP\ffmpeg_x\" -Force; Copy-Item (Get-ChildItem \"$env:TEMP\ffmpeg_x\" -Recurse -Filter ffmpeg.exe | Select-Object -First 1).FullName bin\ffmpeg\ffmpeg.exe; Write-Host '  FFmpeg ok' }" ^
  "if (!(Test-Path 'livekit\livekit-server.exe')) { New-Item -ItemType Directory -Force livekit | Out-Null; $r=Invoke-RestMethod 'https://api.github.com/repos/livekit/livekit/releases/latest'; $a=$r.assets | Where-Object name -like '*windows_amd64.zip' | Select-Object -First 1; $z=\"$env:TEMP\livekit.zip\"; Invoke-WebRequest $a.browser_download_url -OutFile $z; Expand-Archive $z \"$env:TEMP\livekit_x\" -Force; Copy-Item (Get-ChildItem \"$env:TEMP\livekit_x\" -Recurse -Filter livekit-server.exe | Select-Object -First 1).FullName livekit\livekit-server.exe; Write-Host '  LiveKit local ok' }"

echo [6/6] Arquivo de chaves (.env)...
if not exist ".env" (copy ".env.example" ".env" >nul & echo   Criado o .env: abra e coloque suas chaves.) else (echo   .env ja existe: nao mexi.)

echo.
echo Pronto. Agora:
echo   1) abra o arquivo .env e preencha GEMINI_API_KEY e o LiveKit (veja INSTALACAO.md)
echo   2) de dois cliques em Kira.bat
echo.
pause
