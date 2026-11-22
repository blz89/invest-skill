# One-shot installer for the "invest" DSH skill (Windows / PowerShell 5.1+).
#
# Usage (agent-friendly, no repo checkout needed):
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/blz89/invest-skill/main/install.ps1)))
#
# What it does:
#   1. clone or update https://github.com/blz89/invest-skill into the DSH skills dir
#   2. create the bundled Python venv (stock\.venv)
#   3. install requirements.txt, the bundled stock/ package (editable) and pypdf
#   4. run the offline self-test suite (scripts/selftest_all.py --fast)
#
# Options:
#   -Dir <path>   install directory (default: %USERPROFILE%\.dsh\skills\invest)
#   -Ref <ref>    branch or tag (default: main)
#   -SkipTest     skip the offline self-test
#
# Exit code: 0 = installed and verified, non-zero = failed (safe to fail closed).

[CmdletBinding()]
param(
    [string]$Dir = (Join-Path $env:USERPROFILE '.dsh\skills\invest'),
    [string]$Ref = 'main',
    [switch]$SkipTest
)

$ErrorActionPreference = 'Stop'
$RepoUrl = 'https://github.com/blz89/invest-skill.git'

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "OK  $Message" -ForegroundColor Green }
function Stop-Install([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

# --- prerequisites ---------------------------------------------------------
$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitCmd) { Stop-Install 'git not found in PATH (install Git for Windows first)' }

$python = $null
$pythonPre = @()
$cmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $cmd) { $cmd = Get-Command python3 -ErrorAction SilentlyContinue }
if ($cmd) {
    $python = $cmd.Source
} else {
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source; $pythonPre = @('-3') }
}
if (-not $python) { Stop-Install 'Python 3.10+ not found in PATH' }

& $python @pythonPre -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { Stop-Install 'Python 3.10+ required' }

# --- clone or update -------------------------------------------------------
$parent = Split-Path -Parent $Dir
if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }

if (Test-Path (Join-Path $Dir '.git')) {
    Write-Step "Updating existing checkout at $Dir"
    & git -C $Dir fetch --depth 1 origin $Ref
    if ($LASTEXITCODE -ne 0) { Stop-Install "git fetch failed (ref: $Ref)" }
    & git -C $Dir checkout -q FETCH_HEAD
    if ($LASTEXITCODE -ne 0) { Stop-Install 'git checkout failed' }
} else {
    if (Test-Path $Dir) {
        if (@(Get-ChildItem -Force $Dir).Count -gt 0) {
            Stop-Install "$Dir exists and is not empty; remove it or pass -Dir <other path>"
        }
        Remove-Item $Dir -Force
    }
    Write-Step "Cloning $RepoUrl ($Ref) into $Dir"
    & git clone --depth 1 --branch $Ref $RepoUrl $Dir
    if ($LASTEXITCODE -ne 0) { Stop-Install 'git clone failed' }
}

# --- python environment ----------------------------------------------------
$venv = Join-Path $Dir 'stock\.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Step 'Creating Python venv: stock\.venv'
    & $python @pythonPre -m venv $venv
    if ($LASTEXITCODE -ne 0) { Stop-Install 'failed to create venv (is the "venv" module available?)' }
}

Write-Step 'Installing Python dependencies'
& $venvPython -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Stop-Install 'pip upgrade failed' }

& $venvPython -m pip install -r (Join-Path $Dir 'requirements.txt') --quiet
if ($LASTEXITCODE -ne 0) { Stop-Install 'pip install -r requirements.txt failed' }

& $venvPython -m pip install -e (Join-Path $Dir 'stock') --quiet
if ($LASTEXITCODE -ne 0) { Stop-Install 'pip install -e stock failed' }

# pypdf is optional at runtime (only cninfo.py --body needs it) but small and
# used by the test suite, so install it to keep the offline checks green.
& $venvPython -m pip install pypdf --quiet
if ($LASTEXITCODE -ne 0) { Stop-Install 'pip install pypdf failed' }

# --- verify ----------------------------------------------------------------
if (-not $SkipTest) {
    Write-Step 'Running offline self-test (python scripts/selftest_all.py --fast)'
    $oldOffline = $env:A_SHARE_OFFLINE
    $oldEncoding = $env:PYTHONIOENCODING
    $oldTqdm = $env:TQDM_DISABLE
    $env:A_SHARE_OFFLINE = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:TQDM_DISABLE = '1'
    Push-Location $Dir
    try {
        & $venvPython (Join-Path $Dir 'scripts\selftest_all.py') --fast
    } finally {
        Pop-Location
        $env:A_SHARE_OFFLINE = $oldOffline
        $env:PYTHONIOENCODING = $oldEncoding
        $env:TQDM_DISABLE = $oldTqdm
    }
    if ($LASTEXITCODE -ne 0) { Stop-Install 'offline self-test failed' }
}

Write-Host ''
Write-Ok "installed: $Dir"
Write-Ok "python:    $venvPython"
Write-Host 'DSH will pick it up as skill "invest" on the next session.'
Write-Host "Try: & `"$venvPython`" `"$(Join-Path $Dir 'scripts\run.py')`" --capital 50000"
