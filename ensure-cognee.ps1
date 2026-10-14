param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [Parameter(Mandatory = $true)]
    [string]$TokenFile,
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$healthUri = "http://127.0.0.1:8000/health"
$loginUri = "http://127.0.0.1:8000/api/v1/auth/login"
$composeFile = "evals/services/cognee.compose.yml"

function Test-CogneeReady {
    try {
        $result = Invoke-RestMethod -Method Get -Uri $healthUri -TimeoutSec 2
        return $result.status -eq "ready"
    }
    catch {
        return $false
    }
}

function Fail-Cognee([string]$Message) {
    [Console]::Error.WriteLine("[FunAgent] ERROR: $Message")
    exit 1
}

if (-not (Test-CogneeReady)) {
    $docker = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($null -eq $docker) {
        Fail-Cognee "docker.exe is unavailable; Cognee was not started."
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    try {
        $process = Start-Process -FilePath $docker.Source -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
            -ArgumentList @("compose", "-p", "funagent-eval", "-f", $composeFile, "up", "-d")
        $remaining = [Math]::Max(1, [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        if (-not $process.WaitForExit($remaining)) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            Fail-Cognee "Cognee start command exceeded ${TimeoutSeconds}s."
        }
        if ($process.ExitCode -ne 0) {
            Fail-Cognee "Docker Compose could not start Cognee (exit code $($process.ExitCode))."
        }
    }
    catch {
        Fail-Cognee "Docker Compose could not start Cognee: $($_.Exception.Message)"
    }

    while ([DateTime]::UtcNow -lt $deadline -and -not (Test-CogneeReady)) {
        Start-Sleep -Milliseconds 750
    }
    if (-not (Test-CogneeReady)) {
        Fail-Cognee "Cognee did not become ready within ${TimeoutSeconds}s."
    }
}

try {
    $login = Invoke-RestMethod -Method Post -Uri $loginUri -TimeoutSec 5 `
        -ContentType "application/x-www-form-urlencoded" `
        -Body @{ username = "default_user@example.com"; password = "local_eval_only" }
    if ([string]::IsNullOrWhiteSpace($login.access_token)) {
        Fail-Cognee "Cognee login returned no access token."
    }
    $directory = Split-Path -Parent $TokenFile
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    [IO.File]::WriteAllText($TokenFile, "COGNEE_MEMORY_TOKEN=$($login.access_token)`r`n", [Text.UTF8Encoding]::new($false))
}
catch {
    Fail-Cognee "Cognee is ready, but login failed: $($_.Exception.Message)"
}

Write-Host "[FunAgent] Cognee is ready."
exit 0
