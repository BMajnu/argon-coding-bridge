<#
.SYNOPSIS
    One-click PowerShell wrapper for the Gemini 4 Argon Coding Bridge and Account-Pool Login Assistant.
.DESCRIPTION
    Delegates coding, debugging, and refactoring tasks to dispatch_argon.py or launches the
    Tkinter multi-account pool login assistant (login_pool_accounts.py).
.PARAMETER Task
    Description of the coding, bug-fix, or refactoring task to dispatch to Gemini 4 Argon.
.PARAMETER ProjectPath
    Root path of the target project. Defaults to the current working directory.
.PARAMETER Mode
    Target workspace mode: 'gemini' (https://gemini.google.com/app). Google AI Studio
    was removed because it consistently fails on agentic work.
.PARAMETER AutoApply
    Automatically apply the returned apply_changes.ps1 patch upon completion.
.PARAMETER Headless
    Run the browser session in headless mode.
.PARAMETER Slot
    Specific account pool slot (e.g., '/u/0/', '/u/1/') to select inside the persistent profile.
.PARAMETER LoginAssistant
    Launch the Tkinter Account-Pool Login Assistant GUI (login_pool_accounts.py).
.PARAMETER ApplyOnly
    Skip dispatching. Find the delivered apply_changes*.ps1, validate it, and apply it to
    ProjectPath in a single run (Run-ArgonPatch.ps1).
.PARAMETER PatchFile
    Explicit path to the delivered patch script. Used with -ApplyOnly; when omitted the file is
    auto-discovered in the usual download folders.
.PARAMETER ManualSetup
    Assume the workspace is already switched to Task mode with 4 Argon selected. The dispatcher
    still attaches the zip and sends the prompt.
#>
[CmdletBinding()]
param(
    [string]$Task = "",
    [string]$ProjectPath = (Get-Location).Path,
    [ValidateSet("gemini")]
    [string]$Mode = "gemini",
    [switch]$AutoApply,
    [switch]$Headless,
    [string]$Slot = "",
    [switch]$LoginAssistant,
    [switch]$ApplyOnly,
    [string]$PatchFile = "",
    [switch]$ManualSetup,
    [switch]$NewSession,
    [switch]$Resume,
    [int]$MaxAccounts = 0,
    [int]$MaxVerifyRounds = 3,
    [int]$VerifyTimeout = 900,
    [string]$VerifyCmd = "",
    [switch]$SkipVerify,
    [switch]$Rollback,
    [switch]$Dual,
    [string]$RecordWinner = "",
    [switch]$OpenComparison,
    [switch]$MergePatches,
    [string]$FollowupPatch = "",
    [string]$NewPatch = "",
    [string]$ConflictChoices = "",
    [string]$SecondSlot = "",
    [switch]$Force,
    [int]$DeadlineSeconds = 1800,
    [switch]$RegisterTaskStart,
    [switch]$RegisterTaskDone,
    [string]$TaskLabel = "",
    [switch]$ListTasks
)

$ErrorActionPreference = "Stop"

$scriptsDir = $PSScriptRoot
$loginScript = Join-Path $scriptsDir "login_pool_accounts.py"
$dispatchScript = Join-Path $scriptsDir "dispatch_argon.py"
$dualScript = Join-Path $scriptsDir "dual_dispatch.py"
$compareScript = Join-Path $scriptsDir "compare_argon_patches.py"
$applyRunner = Join-Path $scriptsDir "Run-ArgonPatch.ps1"

if ($ListTasks) {
    & python $dispatchScript --list-tasks
    exit $LASTEXITCODE
}

if ($RegisterTaskDone) {
    $doneArgs = @($dispatchScript, "--project-path", $ProjectPath, "--register-task-done")
    if (-not [string]::IsNullOrWhiteSpace($Slot)) {
        $doneArgs += @("--slot", $Slot)
    }
    & python @doneArgs
    exit $LASTEXITCODE
}

if ($RegisterTaskStart) {
    $startArgs = @($dispatchScript, "--project-path", $ProjectPath, "--register-task-start")
    if (-not [string]::IsNullOrWhiteSpace($Slot)) {
        $startArgs += @("--slot", $Slot)
    }
    if (-not [string]::IsNullOrWhiteSpace($TaskLabel)) {
        $startArgs += @("--task-label", $TaskLabel)
    } elseif (-not [string]::IsNullOrWhiteSpace($Task)) {
        $startArgs += @("--task-label", $Task)
    }
    if ($NewSession) {
        $startArgs += "--new-session"
    }
    & python @startArgs
    exit $LASTEXITCODE
}

if (-not [string]::IsNullOrWhiteSpace($RecordWinner)) {
    Write-Host "[+] Recording winning lane '$RecordWinner' for: $ProjectPath" -ForegroundColor Cyan
    & python $dispatchScript --project-path $ProjectPath --record-winner $RecordWinner
    exit $LASTEXITCODE
}

if (($OpenComparison -or $MergePatches) -and [string]::IsNullOrWhiteSpace($Task) -and -not $Dual) {
    if (-not (Test-Path $compareScript)) {
        Write-Error "Comparison script not found at: $compareScript"
        exit 1
    }
    $cmpArgs = @($compareScript, "--project-path", $ProjectPath)
    if (-not [string]::IsNullOrWhiteSpace($FollowupPatch)) {
        $cmpArgs += @("--followup-patch", $FollowupPatch)
    }
    if (-not [string]::IsNullOrWhiteSpace($NewPatch)) {
        $cmpArgs += @("--new-patch", $NewPatch)
    }
    if ($OpenComparison) {
        $cmpArgs += "--open"
    }
    if ($MergePatches) {
        $cmpArgs += "--merge"
    }
    if (-not [string]::IsNullOrWhiteSpace($ConflictChoices)) {
        $cmpArgs += @("--choices", $ConflictChoices)
    }
    if ($AutoApply -or $ApplyOnly) {
        $cmpArgs += "--apply"
    }
    Write-Host "[+] Running Argon Dual-Session Patch Comparison..." -ForegroundColor Cyan
    & python @cmpArgs
    exit $LASTEXITCODE
}

if ($LoginAssistant) {
    if (-not (Test-Path $loginScript)) {
        Write-Error "Login assistant script not found at: $loginScript"
        exit 1
    }
    Write-Host "[+] Launching Gemini 4 Argon Account Pool Login Assistant..." -ForegroundColor Cyan
    & python $loginScript
    exit $LASTEXITCODE
}

if ($Rollback) {
    if (-not (Test-Path $applyRunner)) {
        Write-Error "Apply runner not found at: $applyRunner"
        exit 1
    }
    $shell = if (Get-Command pwsh -ErrorAction SilentlyContinue) { "pwsh" } else { "powershell" }
    Write-Host "[+] Rolling back the most recent Argon patch in: $ProjectPath" -ForegroundColor Yellow
    & $shell -NoProfile -ExecutionPolicy Bypass -File $applyRunner -ProjectPath $ProjectPath -Rollback
    exit $LASTEXITCODE
}

if ($ApplyOnly) {
    if (-not (Test-Path $applyRunner)) {
        Write-Error "Apply runner not found at: $applyRunner"
        exit 1
    }
    $applyArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $applyRunner,
                   "-ProjectPath", $ProjectPath)
    if (-not [string]::IsNullOrWhiteSpace($PatchFile)) {
        $applyArgs += @("-PatchFile", $PatchFile)
    }
    $shell = if (Get-Command pwsh -ErrorAction SilentlyContinue) { "pwsh" } else { "powershell" }
    Write-Host "[+] Applying the delivered Argon patch to: $ProjectPath" -ForegroundColor Cyan
    & $shell @applyArgs
    exit $LASTEXITCODE
}

if ([string]::IsNullOrWhiteSpace($Task) -and -not $Resume) {
    Write-Host "Usage:" -ForegroundColor Yellow
    Write-Host "  .\Invoke-ArgonBridge.ps1 -Task `"Describe bug or feature`" [-ProjectPath .] [-Mode gemini] [-Slot /u/0/] [-AutoApply] [-Headless]" -ForegroundColor White
    Write-Host "  .\Invoke-ArgonBridge.ps1 -Resume [-ProjectPath .] [-AutoApply]   # resume a closed browser task" -ForegroundColor White
    Write-Host "  .\Invoke-ArgonBridge.ps1 -Task `"...`" -ManualSetup   # you already switched to Task + 4 Argon" -ForegroundColor White
    Write-Host "  .\Invoke-ArgonBridge.ps1 -LoginAssistant" -ForegroundColor White
    Write-Host "  .\Invoke-ArgonBridge.ps1 -ApplyOnly [-PatchFile <path>]   # apply the delivered patch only" -ForegroundColor White
    exit 1
}

if (-not (Test-Path $dispatchScript)) {
    Write-Error "Dispatcher script not found at: $dispatchScript"
    exit 1
}

$argList = @(
    $dispatchScript,
    "--task", $Task,
    "--project-path", $ProjectPath,
    "--mode", $Mode
)

if (-not [string]::IsNullOrWhiteSpace($Slot)) {
    $argList += @("--slot", $Slot)
}
if ($AutoApply) {
    $argList += "--auto-apply"
}
if ($ManualSetup) {
    $argList += "--manual-setup"
}
if ($Headless) {
    $argList += "--headless"
}
if ($NewSession) {
    $argList += "--new-session"
}
if ($Resume) {
    $argList += "--resume"
}
if ($MaxAccounts -gt 0) {
    $argList += @("--max-accounts", "$MaxAccounts")
}
if ($VerifyTimeout -gt 0) {
    $argList += @("--verify-timeout", "$VerifyTimeout")
}
if (-not [string]::IsNullOrWhiteSpace($VerifyCmd)) {
    $argList += @("--verify-cmd", $VerifyCmd)
}
if ($SkipVerify) {
    $argList += "--skip-verify"
}
if ($Dual) {
    $argList += "--dual"
}
if (-not [string]::IsNullOrWhiteSpace($SecondSlot)) {
    $argList += @("--second-slot", $SecondSlot)
}
if ($OpenComparison) {
    $argList += "--open-comparison"
}
if ($Force) {
    $argList += "--force"
}
if ($DeadlineSeconds -gt 0) {
    $argList += @("--deadline-seconds", "$DeadlineSeconds")
}

Write-Host "[+] Invoking Gemini 4 Argon Dispatcher ($Mode)..." -ForegroundColor Cyan
& python @argList
exit $LASTEXITCODE
