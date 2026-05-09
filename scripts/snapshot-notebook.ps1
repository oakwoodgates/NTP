<#
.SYNOPSIS
Run a notebook headless and produce timestamped snapshots.

.DESCRIPTION
Writes the executed notebook + a rendered HTML copy to:
  reports/notebooks/<category>/<name>_<UTC_TIMESTAMP>.ipynb
  reports/html/<category>/<name>_<UTC_TIMESTAMP>.html

Where <category> is the parent directory name of the input notebook
(so e.g. notebooks\backtest\ma_cross.ipynb -> category=backtest).

Why this exists: when you click "Run All" inside Jupyter / VS Code /
Cursor, the in-notebook "save" cell runs against the *stale* on-disk
file because cell outputs haven't been autosaved yet -- producing an
empty/incomplete snapshot.  This script avoids the race entirely:
nbconvert manages the kernel itself and writes the executed copy
atomically.

.PARAMETER NotebookPath
Path to the notebook to snapshot.

.PARAMETER OutputBasename
Optional: custom basename for the output files (no extension).
Default: derived from the input filename.

.EXAMPLE
.\scripts\snapshot-notebook.ps1 notebooks\backtest\ma_cross.ipynb

.EXAMPLE
.\scripts\snapshot-notebook.ps1 notebooks\backtest\ma_cross.ipynb my_run

.NOTES
Requires: jupyter (already in the project venv).  Activate the venv
first or invoke jupyter via the venv's full path.
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$NotebookPath,

    [Parameter(Mandatory = $false, Position = 1)]
    [string]$OutputBasename = $null
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $NotebookPath -PathType Leaf)) {
    Write-Error "File not found: $NotebookPath"
    exit 1
}

$nbDir   = Split-Path $NotebookPath -Parent
$nbFile  = [System.IO.Path]::GetFileNameWithoutExtension($NotebookPath)
$category = Split-Path $nbDir -Leaf
$ts      = (Get-Date).ToUniversalTime().ToString("yyyyMMdd_HHmmss")

# Project root = parent of scripts/
$scriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir

$nbOutDir   = Join-Path $projectRoot "reports\notebooks\$category"
$htmlOutDir = Join-Path $projectRoot "reports\html\$category"
New-Item -ItemType Directory -Force -Path $nbOutDir   | Out-Null
New-Item -ItemType Directory -Force -Path $htmlOutDir | Out-Null

if ([string]::IsNullOrEmpty($OutputBasename)) {
    $basename = "${nbFile}_$ts"
} else {
    $basename = "${OutputBasename}_$ts"
}

# Locate jupyter.  Walk upwards from the project root looking for a
# .venv/ — handles both standalone projects and git worktrees that
# share the parent project's venv.  Falls back to PATH.
$jupyter = $null
$searchDir = $projectRoot
for ($i = 0; $i -lt 5; $i++) {
    $candidate = Join-Path $searchDir ".venv\Scripts\jupyter.exe"
    if (Test-Path $candidate -PathType Leaf) {
        $jupyter = $candidate
        break
    }
    $candidate = Join-Path $searchDir ".venv\bin\jupyter"
    if (Test-Path $candidate -PathType Leaf) {
        $jupyter = $candidate
        break
    }
    $parent = Split-Path -Parent $searchDir
    if ($parent -eq $searchDir) { break }  # reached fs root
    $searchDir = $parent
}
if ($null -eq $jupyter) {
    $cmd = Get-Command jupyter -ErrorAction SilentlyContinue
    if ($null -ne $cmd) {
        $jupyter = $cmd.Source
    } else {
        Write-Error "jupyter not found.  Searched up to 5 parent dirs from $projectRoot for .venv/, also tried PATH.  Activate the project venv first, or install jupyter."
        exit 1
    }
}
Write-Host "Using jupyter: $jupyter"
Write-Host ""

Write-Host "Executing notebook (this may take a few minutes)..."
Write-Host "  Input : $NotebookPath"
Write-Host "  Output: $(Join-Path $nbOutDir "$basename.ipynb")"
Write-Host ""

& $jupyter nbconvert `
    --execute `
    --to notebook `
    --output-dir $nbOutDir `
    --output $basename `
    $NotebookPath
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Rendering HTML..."
$nbOutPath = Join-Path $nbOutDir "$basename.ipynb"
& $jupyter nbconvert `
    --to html `
    --output-dir $htmlOutDir `
    --output $basename `
    $nbOutPath
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Snapshot saved:" -ForegroundColor Green
Write-Host "  notebook: $nbOutPath"
Write-Host "  html    : $(Join-Path $htmlOutDir "$basename.html")"
