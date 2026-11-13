<#
.SYNOPSIS
    Bring every installed Argon Coding Bridge plugin root in step with the repo.

.DESCRIPTION
    A thin Windows wrapper around scripts/sync_plugin.py, so the plugin can be
    updated with one click (Open_Argon_Update.bat) or from any agent that can
    run PowerShell.

    The runtime credential files are never touched, a MiniMax root never gets a
    generic plugin.json, and nothing is deleted.

.EXAMPLE
    pwsh -File Sync-ArgonPlugin.ps1
    pwsh -File Sync-ArgonPlugin.ps1 -Check
    pwsh -File Sync-ArgonPlugin.ps1 -Repo "D:\my\argon-coding-bridge" -Target "C:\Users\me\.minimax\plugins\argon-coding-bridge"
#>
param(
    [string]$Repo,
    [string[]]$Target,
    [switch]$Check,
    [switch]$DryRun,
    [switch]$Install,
    [switch]$NoGitAlign,
    [switch]$Json,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $scriptDir 'sync_plugin.py'

if (-not (Test-Path -LiteralPath $py)) {
    Write-Host "[ERROR] sync_plugin.py was not found at $py" -ForegroundColor Red
    exit 2
}

$pythonExe = $null
foreach ($cand in @('python', 'python3', 'py')) {
    $found = Get-Command $cand -ErrorAction SilentlyContinue
    if ($found) { $pythonExe = $found.Source; break }
}
if (-not $pythonExe) {
    Write-Host '[ERROR] Python was not found on PATH.' -ForegroundColor Red
    exit 2
}

$argList = @($py)
if ($Repo)     { $argList += @('--repo', $Repo) }
foreach ($t in @($Target)) { if ($t) { $argList += @('--target', $t) } }
if ($Check)    { $argList += '--check' }
if ($DryRun)   { $argList += '--dry-run' }
if ($Install)  { $argList += '--install' }
if ($NoGitAlign) { $argList += '--no-git-align' }
if ($Json)     { $argList += '--json' }
if ($Quiet)    { $argList += '--quiet' }

Write-Host "Running: $pythonExe $($argList -join ' ')"
& $pythonExe @argList
$code = $LASTEXITCODE

Write-Host ''
if ($code -eq 0) {
    Write-Host '[SUCCESS] every installed plugin root is in step with the repo.' -ForegroundColor Green
} else {
    Write-Host "[ERROR] the sync finished with exit code $code." -ForegroundColor Red
}
exit $code
