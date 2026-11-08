@echo off
setlocal
set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"
timeout /t 3 /nobreak >nul
call "%PROJECT_DIR%\Kira.bat"
endlocal
