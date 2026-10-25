@echo off
setlocal
chcp 65001 > nul

echo ===============================================================================
echo   DocumentTheProject - PDF-Dokumentation eines 4diac-Projekts
echo ===============================================================================
echo.

python --version > nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [FEHLER] Python wurde nicht im Pfad gefunden.
    echo.
    pause
    exit /b 1
)

python "%~dp0document_project.py" %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [FEHLER] Die Generierung ist mit einem Fehler abgebrochen.
    echo.
    pause
    exit /b %ERRORLEVEL%
)

echo.
if "%~1"=="" (
    pause
)
endlocal
