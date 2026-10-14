@echo off
setlocal
pushd "%~dp0" || exit /b 1

where uv.exe >nul 2>nul
if errorlevel 1 (
    echo uv.exe is required. Install uv on Windows and add it to PATH. 1>&2
    popd
    exit /b 127
)

set "cognee_token_file=%CD%\.funagent\cognee-session.env"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\ensure-cognee.ps1" -ProjectRoot "%CD%" -TokenFile "%cognee_token_file%" -TimeoutSeconds 30
if errorlevel 1 (
    echo [FunAgent] Cognee startup failed; continuing without a reachable Cognee source. 1>&2
    set "COGNEE_MEMORY_TOKEN=cognee-unavailable"
) else (
    for /f "usebackq tokens=1,* delims==" %%A in ("%cognee_token_file%") do if /i "%%A"=="COGNEE_MEMORY_TOKEN" set "COGNEE_MEMORY_TOKEN=%%B"
    if not defined COGNEE_MEMORY_TOKEN (
        echo [FunAgent] Cognee token was not loaded; continuing without a reachable Cognee source. 1>&2
        set "COGNEE_MEMORY_TOKEN=cognee-unavailable"
    )
)

uv.exe run --locked --extra openai --extra mcp --extra supabase --extra embedding --extra cognee funagent --conf-dir "%CD%" %*
set "exit_code=%ERRORLEVEL%"
popd
exit /b %exit_code%
