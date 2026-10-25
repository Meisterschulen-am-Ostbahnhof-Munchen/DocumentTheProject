@echo off
setlocal
chcp 65001 > nul

rem ===========================================================================
rem   Pfade - nur hier, nicht in document_project.py
rem ===========================================================================
set "GIT_MS=C:\git\ms"
set "PROJECT=%GIT_MS%\4diac-SlurryTanker_AX-sample\Ventilsteuerung\4diacIDE-workspace\test"
set "SVG_TOOL=%GIT_MS%\IEC61499_FunctionBlock_SVG"
rem ISO-Designer-Pool (Objektbaum aus .jop/.jvi, Bilder aus Output\<Pool>.iop)
set "VT_POOL=%GIT_MS%\4diac-SlurryTanker_AX-sample\Ventilsteuerung\ISO-DesignerProjects\Workspace\DefaultPool"
rem list_mask_objects.py
set "POOL_SCRIPTS=%GIT_MS%\4diac_training1\Ventilsteuerung\scripts_central"
rem vt-render aus AgIsoVirtualTerminal (Branch feat/object-pool-screenshots, PR 224), siehe README
set "VT_RENDER=%GIT_MS%\AgIsoVirtualTerminal\build-renderer\VtRender_artefacts\Release\vt-render.exe"

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

set "VT_ARGS=--vt-pool "%VT_POOL%" --pool-scripts "%POOL_SCRIPTS%""
if exist "%VT_RENDER%" (
    set "VT_ARGS=%VT_ARGS% --vt-render "%VT_RENDER%""
) else (
    echo [WARNUNG] %VT_RENDER% fehlt - Bedienoberflaeche ohne Bilder.
)

rem Ohne Parameter: das SlurryTanker-Projekt oben; sonst Projekt und Optionen wie bei document_project.py
if "%~1"=="" (
    python "%~dp0document_project.py" "%PROJECT%" --svg-tool "%SVG_TOOL%" %VT_ARGS%
) else (
    python "%~dp0document_project.py" --svg-tool "%SVG_TOOL%" %*
)
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
