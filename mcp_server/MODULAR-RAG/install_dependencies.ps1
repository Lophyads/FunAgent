[CmdletBinding()]
param(
    [string]$BasePython = "",
    [switch]$SkipPipUpgrade
)

$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$VenvDirectory = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDirectory "Scripts\python.exe"
$RequirementsFile = Join-Path $ProjectRoot "requirements.txt"

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string[]]$CommandArguments
    )

    & $Executable @CommandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Executable $($CommandArguments -join ' ')"
    }
}

function New-ProjectVirtualEnvironment {
    if ($BasePython) {
        $ResolvedBasePython = (Resolve-Path $BasePython).Path
        Invoke-CheckedCommand $ResolvedBasePython @("-m", "venv", $VenvDirectory)
        return
    }

    $PythonLauncher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($PythonLauncher) {
        Invoke-CheckedCommand $PythonLauncher.Source @("-3", "-m", "venv", $VenvDirectory)
        return
    }

    $PythonCommand = Get-Command "python" -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        Invoke-CheckedCommand $PythonCommand.Source @("-m", "venv", $VenvDirectory)
        return
    }

    throw "Python 3.11-3.13 was not found. Install Python or pass -BasePython with the interpreter path."
}

if (-not (Test-Path -LiteralPath $RequirementsFile -PathType Leaf)) {
    throw "Requirements file not found: $RequirementsFile"
}

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    Write-Host "Creating virtual environment at $VenvDirectory ..."
    New-ProjectVirtualEnvironment
}

Write-Host "Using virtual environment: $VenvDirectory"

$VersionCheck = @'
import sys

if not ((3, 11) <= sys.version_info[:2] < (3, 14)):
    raise SystemExit(
        f"Unsupported Python {sys.version_info.major}.{sys.version_info.minor}; "
        "this project requires Python 3.11-3.13."
    )
print(f"Python: {sys.version.split()[0]}")
'@
Invoke-CheckedCommand $VenvPython @("-c", $VersionCheck)

if (-not $SkipPipUpgrade) {
    Write-Host "Updating pip, setuptools, and wheel ..."
    Invoke-CheckedCommand $VenvPython @(
        "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"
    )
}

Write-Host "Installing project dependencies ..."
Invoke-CheckedCommand $VenvPython @(
    "-m", "pip", "install", "--requirement", $RequirementsFile
)

Write-Host "Checking installed modules ..."
$DependencyCheck = @'
from importlib import import_module

modules = (
    "yaml",
    "dotenv",
    "requests",
    "httpx",
    "jieba",
    "langchain_text_splitters",
    "numpy",
    "pandas",
    "PIL",
    "pymupdf4llm",
    "pdfminer.high_level",
    "pdfplumber",
    "fitz",
    "chromadb",
    "openai",
    "mcp",
    "streamlit",
    "datasets",
    "ragas",
    "sklearn",
    "sentence_transformers",
    "torch",
)

failed = []
for module in modules:
    try:
        import_module(module)
    except Exception as exc:
        failed.append(f"{module}: {exc}")

if failed:
    raise SystemExit("Dependency check failed:\n- " + "\n- ".join(failed))

print("Dependency check passed, including PyMuPDF4LLM PDF support.")
'@
Invoke-CheckedCommand $VenvPython @("-c", $DependencyCheck)

Write-Host "All dependencies are installed successfully."
Write-Host "Start the dashboard with:"
Write-Host "  & `"$VenvPython`" scripts\start_dashboard.py"
