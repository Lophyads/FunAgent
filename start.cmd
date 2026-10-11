@echo off
setlocal
pushd "%~dp0" || exit /b 1

where uv.exe >nul 2>nul
if errorlevel 1 (
    echo uv.exe is required. Install uv on Windows and add it to PATH. 1>&2
    popd
    exit /b 127
)

uv.exe run --locked --extra openai --extra mcp --extra supabase --extra embedding funagent --conf-dir "%CD%" %*
set "exit_code=%ERRORLEVEL%"
popd
exit /b %exit_code%
