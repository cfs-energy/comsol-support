<#
.SYNOPSIS
    One-command bootstrap for comsol-support on Windows.

.DESCRIPTION
    The Windows counterpart of scripts/install.sh — same steps, same checks.
    The environment checks live in comsol_support/doctor.py, not here, so the
    three platforms cannot drift apart: this script decides *what to do*, and
    `doctor` decides *whether the machine can do it*.

    Every step is idempotent; re-running is safe and skips completed work.

    What it does NOT do: the Layer C slot harvest
    (`comsol-support scrape slots`) is left to you — it is a second full pass
    over the model library and holds a license seat throughout.

.PARAMETER Check
    Run the preflight only; change nothing.

.PARAMETER NoCorpus
    Skip the .mph corpus mining (the slow part).

.EXAMPLE
    .\scripts\install.ps1
    .\scripts\install.ps1 -Check
    .\scripts\install.ps1 -NoCorpus
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$NoCorpus
)

# Mirror install.sh's `set -e`: halt on exit code, not on stderr text. In
# Windows PowerShell 5.1, $ErrorActionPreference='Stop' wraps every stderr
# line from a native command as a NativeCommandError terminating error - so
# uv's normal progress ("Using CPython 3.12.10 interpreter at:...") aborts
# the install at the first `uv sync`. Every native call below checks
# $LASTEXITCODE explicitly, so 'Continue' is the correct match.
$ErrorActionPreference = 'Continue'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -Path $repoRoot -ErrorAction Stop

function Write-Ok   { param($m) Write-Host "  ok    $m"   -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "  warn  $m"   -ForegroundColor Yellow }
function Write-Fail { param($m) Write-Host "  FAIL  $m"   -ForegroundColor Red }

function Get-Tool {
    # winget installs land in a Links shim directory that an already-open
    # shell does not have on PATH yet - the single most common "I installed
    # it but it is not found" report on Windows. Look there too.
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $roots = @(
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages')
    )
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        $hit = Get-ChildItem $root -Recurse -Filter "$Name.exe" -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

Write-Host "comsol-support install - $repoRoot"
Write-Host ""

# ---- uv -------------------------------------------------------------------
# The one true bootstrap prerequisite: uv provisions Python itself, so it is
# checked here rather than in doctor (which needs a Python to run at all).
$uv = Get-Tool 'uv'
if (-not $uv) {
    Write-Fail "uv not installed - https://docs.astral.sh/uv/"
    Write-Fail "  winget install astral-sh.uv"
    Write-Fail "  (then open a NEW terminal so PATH picks it up)"
    exit 1
}
Write-Ok "uv: $(& $uv --version)"

# Corporate TLS-inspecting proxies re-sign PyPI; uv then rejects the chain
# with 'invalid peer certificate: UnknownIssuer'. Trusting the Windows
# certificate store fixes it and is a no-op elsewhere. (uv <0.9 spells this
# UV_NATIVE_TLS; that name is deprecated and now warns, so it is not set.)
if (-not $env:UV_SYSTEM_CERTS) { $env:UV_SYSTEM_CERTS = '1' }

# ---- preflight ------------------------------------------------------------
# Prefer a system Python so -Check truly changes nothing (no venv created).
$preflight = $null
foreach ($name in @('python', 'python3', 'py')) {
    $exe = Get-Tool $name
    if (-not $exe) { continue }
    try {
        & $exe -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $preflight = $exe; break }
    } catch { }
}

Write-Host ""
if ($preflight) {
    & $preflight scripts\doctor.py
} else {
    Write-Warn "no system Python 3.10+; using uv's"
    & $uv run python scripts\doctor.py
}
if ($LASTEXITCODE -ne 0) { exit 1 }

if ($Check) {
    Write-Host ""
    Write-Host "Preflight passed. (-Check: nothing was installed.)"
    exit 0
}

# ---- install --------------------------------------------------------------
Write-Host ""
Write-Host "Installing Python package"
# Defensive: a prior attempt that died mid-sync (Ctrl-C, disk full, or the
# NativeCommandError trap we now avoid) can leave an empty .venv with no
# python.exe. uv then refuses it with "not a valid Python environment" on
# every re-run until the user knows to delete it by hand.
if ((Test-Path .venv) -and -not (Test-Path .venv\Scripts\python.exe)) {
    Write-Warn ".venv from an earlier attempt has no python.exe - removing"
    Remove-Item -Recurse -Force .venv
}
& $uv sync --extra dev
if ($LASTEXITCODE -ne 0) { exit 1 }
Write-Ok "uv sync complete"

Write-Host ""
Write-Host "Compiling COMSOL-dependent Java"
$compile = @'
import comsol_support
from comsol_support.java_facade import JavaFacade, javac_diagnostics

jf = JavaFacade(comsol_path=comsol_support.COMSOL_PATH, workspace_dir="corpus")
result = jf.compile_comsol_sources()
if result.success:
    print(f"  compiled every source in {jf.java_source_dir}")
else:
    print("Compilation failed:")
    print(javac_diagnostics(result.stderr) or result.stderr[:800])
    raise SystemExit(1)
'@
$compile | & $uv run python -
if ($LASTEXITCODE -ne 0) { exit 1 }
Write-Ok "Java sources compile against the COMSOL classpath"

Write-Host ""
Write-Host "Building the knowledge base"
if ((Test-Path data\comsol.db) -and (Get-Item data\comsol.db).Length -gt 0) {
    Write-Warn "data\comsol.db already exists - scrapers are additive, skipping"
    Write-Warn "delete it first if you want a clean rebuild"
} else {
    & $uv run comsol-support scrape javadoc
    if (Get-Tool 'pdftotext') {
        & $uv run comsol-support scrape refmanual
        Write-Ok "Javadoc + Reference Manual ingested"
    } else {
        Write-Warn "pdftotext not found - skipping the Reference Manual"
        Write-Warn "winget install oschwartz10612.Poppler, then re-run:"
        Write-Warn "  uv run comsol-support scrape refmanual"
    }
}

if (-not $NoCorpus) {
    Write-Host ""
    Write-Host "Mining the .mph corpus (~10 minutes; holds a license seat)"
    # Bounded probe first. Without it, a busy license server turns the
    # last step of a first install into an indefinite hang with no
    # explanation - the worst possible first impression.
    & $uv run comsol-support license-status --timeout 60 *> $null
    if ($LASTEXITCODE -eq 0) {
        & $uv run comsol-support scrape corpus
        Write-Ok "corpus mined"
    } else {
        Write-Warn "no COMSOL license seat free right now - skipping corpus mining"
        Write-Warn "everything else is installed; run this when a seat frees up:"
        Write-Warn "  uv run comsol-support scrape corpus"
    }
} else {
    Write-Warn "skipped corpus mining (-NoCorpus); 'fragments' table stays empty"
}

Write-Host ""
Write-Host "Verifying"
& $uv run pytest -q

Write-Host ""
Write-Host "Final check"
& $uv run comsol-support doctor

Write-Host ""
Write-Host "Done."
Write-Host ""
Write-Host "  Knowledge base : data\comsol.db"
Write-Host "  Diagnostics    : uv run comsol-support doctor"
Write-Host "  Next (optional): uv run comsol-support scrape slots -o data\slots_dump.jsonl"
Write-Host "                   populates the Layer C catalog; a second full COMSOL pass."
Write-Host ""
