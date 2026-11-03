<#
.SYNOPSIS
    Finds, validates and runs the PowerShell patch file delivered by Gemini 4 Argon.
.DESCRIPTION
    Gemini 4 Argon returns its work as a downloadable PowerShell file (apply_changes.ps1).
    Clicking the download in the Gemini web UI drops that file into the browser download
    folder, often renamed to something like "apply_changes (1).ps1".

    This runner locates the delivered file, validates that it is intact (non empty, no
    UTF-8 BOM, and parseable), copies it into the target project and executes it with the
    project root as the working directory. One double click applies and verifies the whole
    patch - no hunting through chat text and no hand copying of code blocks.

    Runs on Windows PowerShell 5.1 and PowerShell 7.
.PARAMETER ProjectPath
    Target codebase root. Defaults to the repository root that contains this skill.
.PARAMETER PatchFile
    Explicit path to the delivered patch script. When omitted the file is auto discovered.
.PARAMETER SearchPath
    Extra folders to search for the delivered patch script.
.PARAMETER DryRun
    Find and validate the patch and report what would run, without changing anything.
.PARAMETER Rollback
    Restore the newest .argon_backup session instead of applying a patch.
.EXAMPLE
    .\Run-ArgonPatch.ps1 -ProjectPath "C:\MyProject"
.EXAMPLE
    .\Run-ArgonPatch.ps1 -ProjectPath "C:\MyProject" -PatchFile "$env:USERPROFILE\Downloads\apply_changes (1).ps1" -DryRun
#>
[CmdletBinding()]
param(
    [string]$ProjectPath = (Join-Path $PSScriptRoot "..\..\.."),
    [string]$PatchFile = "",
    [string[]]$SearchPath = @(),
    [switch]$DryRun,
    [switch]$Rollback
)

$ErrorActionPreference = "Stop"

# --------------------------------------------------------------- helpers ----

function Write-ArgonInfo([string]$Message) {
    Write-Host "[+] $Message" -ForegroundColor Cyan
}

function Write-ArgonWarn([string]$Message) {
    Write-Host "[!] $Message" -ForegroundColor Yellow
}

function Write-ArgonError([string]$Message) {
    Write-Host "[ERROR] $Message" -ForegroundColor Red
}

# Returns the newest file matching $Filter inside $Folder, or $null.
function Get-NewestPatchIn([string]$Folder, [string]$Filter) {
    if ([string]::IsNullOrWhiteSpace($Folder)) { return $null }
    if (-not (Test-Path -LiteralPath $Folder -PathType Container)) { return $null }
    $items = @(Get-ChildItem -LiteralPath $Folder -Filter $Filter -File -ErrorAction SilentlyContinue)
    # Archived copies of earlier rounds and the runner's own backups are
    # leftovers, not deliveries. Picking one of them would re-apply a patch
    # that was already applied.
    $items = @($items | Where-Object {
        $_.Name -notlike "*.bak-*" -and
        $_.Name -notlike "apply_changes_round*" -and
        $_.Name -notlike "apply_changes *round*"
    })
    if ($items.Count -eq 0) { return $null }
    $newest = ($items | Sort-Object -Property LastWriteTime -Descending | Select-Object -First 1)
    if (-not $newest) { return $null }
    return $newest.FullName
}

# Resolves a path to its long form. Resolve-Path can hand back the 8.3 short name
# (e.g. C:\Users\TECHZO~1\...) while Get-ChildItem reports long names
# (e.g. C:\Users\TechZone IT\...), which would corrupt every relative path below.
function Get-LongPath([string]$Path) {
    try {
        return (Get-Item -LiteralPath (Resolve-Path -LiteralPath $Path).Path).FullName
    } catch {
        return $Path
    }
}

try {
    $scriptRoot = $PSScriptRoot
    if (-not $scriptRoot) { $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path }

    # ------------------------------------------------------------- banner ----

    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host "  Gemini 4 Argon Patch Runner" -ForegroundColor Cyan
    Write-Host "==========================================" -ForegroundColor Cyan

    # ------------------------------------------------- resolve project root --

    if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
        $ProjectPath = Join-Path $scriptRoot "..\..\.."
    }

    try {
        $projectRoot = (Get-Item -LiteralPath (Resolve-Path -LiteralPath $ProjectPath).Path).FullName
    } catch {
        Write-ArgonError "Project path does not exist or cannot be resolved: $ProjectPath"
        exit 1
    }

    $runnerPath = $PSCommandPath
    if (-not $runnerPath) { $runnerPath = Join-Path $scriptRoot "Run-ArgonPatch.ps1" }
    $applyScript = Join-Path $scriptRoot "Apply-ArgonPatch.ps1"

    $downloadsDir = Join-Path $env:USERPROFILE "Downloads"
    $projectDownloadsDir = Join-Path $projectRoot "downloads"

    # Folders that were inspected while looking for the delivered patch file.
    $searchedFolders = New-Object System.Collections.Generic.List[string]
    function Add-SearchedFolder([string]$Path) {
        if ([string]::IsNullOrWhiteSpace($Path)) { return }
        $full = Get-LongPath $Path
        if (-not $searchedFolders.Contains($full)) { $searchedFolders.Add($full) }
    }
    Add-SearchedFolder $projectRoot
    Add-SearchedFolder $projectDownloadsDir
    Add-SearchedFolder $downloadsDir
    foreach ($extra in $SearchPath) { Add-SearchedFolder $extra }

    # ---------------------------------------------------- locate the patch --

    $foundPatch = $null

    if (-not [string]::IsNullOrWhiteSpace($PatchFile)) {
        # (a) explicit patch file - it must exist
        if (-not (Test-Path -LiteralPath $PatchFile -PathType Leaf)) {
            Write-ArgonError "The -PatchFile you specified does not exist: $PatchFile"
            Write-Host "  Pass the full path of the delivered apply_changes.ps1, e.g." -ForegroundColor Gray
            Write-Host "  -PatchFile `"$env:USERPROFILE\Downloads\apply_changes (1).ps1`"" -ForegroundColor Gray
            exit 2
        }
        $foundPatch = Get-LongPath $PatchFile
    }
    else {
        # Collect EVERY candidate from EVERY known location and take the newest.
        # A freshly delivered download must beat a stale copy that happens to sit
        # in the project root from an earlier run.
        $candidates = New-Object System.Collections.Generic.List[object]

        # An explicit file passed through -SearchPath wins outright.
        foreach ($extra in $SearchPath) {
            if ([string]::IsNullOrWhiteSpace($extra)) { continue }
            if (Test-Path -LiteralPath $extra -PathType Leaf) {
                $foundPatch = Get-LongPath $extra
                break
            }
        }

        if (-not $foundPatch) {
            # NOTE: the copy of the patch that THIS runner writes into the
            # project root is always the PREVIOUS patch, never a new delivery,
            # so it is not a candidate here. Only a real Gemini delivery counts:
            # apply_changes.ps1 / apply_changes (1).ps1 and friends. Archived
            # copies (apply_changes_roundN.ps1) and *.bak-* backups are
            # leftovers and are ignored on purpose.
            foreach ($pair in @(
                @($projectDownloadsDir, "apply_changes*.ps1"),
                @($downloadsDir, "apply_changes*.ps1"),
                @($downloadsDir, "*argon*.ps1")
            )) {
                $newest = Get-NewestPatchIn $pair[0] $pair[1]
                if ($newest) { $candidates.Add((Get-Item -LiteralPath $newest)) }
            }
            foreach ($extra in $SearchPath) {
                if ([string]::IsNullOrWhiteSpace($extra)) { continue }
                if (Test-Path -LiteralPath $extra -PathType Leaf) { continue }
                $newest = Get-NewestPatchIn $extra "apply_changes*.ps1"
                if ($newest) { $candidates.Add((Get-Item -LiteralPath $newest)) }
            }

            if ($candidates.Count -gt 0) {
                $best = $candidates | Sort-Object -Property LastWriteTime -Descending | Select-Object -First 1
                if ($best) { $foundPatch = Get-LongPath $best.FullName }
            }
        }
    }

    # -------------------------------------------------------- status banner --

    Write-Host ""
    Write-Host "Target directory : $projectRoot" -ForegroundColor Gray
    if ($Rollback) {
        Write-Host "Patch file       : (rollback mode - not applicable)" -ForegroundColor Yellow
    }
    elseif ($foundPatch) {
        Write-Host "Patch file       : $foundPatch" -ForegroundColor Green
    }
    else {
        Write-Host "Patch file       : NOT FOUND" -ForegroundColor Yellow
    }
    Write-Host "Searched in      :" -ForegroundColor Gray
    foreach ($folder in $searchedFolders) {
        Write-Host "                    $folder" -ForegroundColor DarkGray
    }
    Write-Host "Patterns         : apply_changes.ps1 | downloads\*.ps1 | Downloads\apply_changes*.ps1 | Downloads\*argon*.ps1 | <extra folder>\*.ps1" -ForegroundColor DarkGray
    Write-Host ""

    # ------------------------------------------------------ rollback mode ----

    if ($Rollback) {
        if (-not (Test-Path -LiteralPath $applyScript -PathType Leaf)) {
            Write-ArgonError "Rollback helper not found: $applyScript"
            exit 1
        }

        $shellExe = $null
        $pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
        if ($pwshCmd) { $shellExe = $pwshCmd.Source }
        if (-not $shellExe) {
            $winCmd = Get-Command powershell -ErrorAction SilentlyContinue
            if ($winCmd) { $shellExe = $winCmd.Source }
        }
        if (-not $shellExe) {
            Write-ArgonError "Neither pwsh nor powershell could be found on PATH."
            exit 1
        }

        Write-ArgonWarn "Rollback requested - restoring the newest .argon_backup session."
        $rollbackArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $applyScript, "-ProjectPath", $projectRoot, "-Rollback")
        $previousEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $shellExe @rollbackArgs 2>&1 | ForEach-Object { Write-Host ("    " + $_.ToString()) }
            $rollbackCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousEap
        }
        if ($null -eq $rollbackCode) { $rollbackCode = 0 }
        Write-Host ""
        if ($rollbackCode -eq 0) {
            Write-ArgonInfo "Rollback finished successfully (exit code 0)."
        } else {
            Write-ArgonError "Rollback failed with exit code $rollbackCode."
        }
        exit $rollbackCode
    }

    # ------------------------------------------------ nothing found (exit 2) --

    if (-not $foundPatch) {
        Write-ArgonError "No delivered patch script was found."
        Write-Host ""
        Write-Host "  Folders that were searched:" -ForegroundColor Gray
        foreach ($folder in $searchedFolders) {
            Write-Host "    - $folder" -ForegroundColor DarkGray
        }
        Write-Host ""
        Write-Host "  Download apply_changes.ps1 from the Gemini 4 Argon chat, or point" -ForegroundColor Gray
        Write-Host "  this script straight at the file:" -ForegroundColor Gray
        Write-Host "    -PatchFile `"C:\path\to\apply_changes (1).ps1`"" -ForegroundColor White
        Write-Host ""
        exit 2
    }

    # --------------------------------------------------------- validation ----

    $patchInfo = Get-Item -LiteralPath $foundPatch
    if ($patchInfo.Length -le 0) {
        Write-ArgonError "The delivered patch file is empty (0 bytes): $foundPatch"
        Write-Host "  The download looks incomplete - download it again before applying." -ForegroundColor Gray
        exit 3
    }
    Write-ArgonInfo "Patch file size: $($patchInfo.Length) bytes"

    # Strip a UTF-8 BOM if the delivered file has one, then rewrite it without.
    $rawBytes = [System.IO.File]::ReadAllBytes($foundPatch)
    if ($rawBytes.Length -ge 3 -and $rawBytes[0] -eq 0xEF -and $rawBytes[1] -eq 0xBB -and $rawBytes[2] -eq 0xBF) {
        $bomText = [System.Text.Encoding]::UTF8.GetString($rawBytes, 3, $rawBytes.Length - 3)
        [System.IO.File]::WriteAllText($foundPatch, $bomText, [System.Text.UTF8Encoding]::new($false))
        Write-ArgonWarn "Removed the UTF-8 BOM from the delivered patch file."
    }

    # Parse check - never run a truncated or corrupt file.
    $errs = $null
    $toks = $null
    [System.Management.Automation.Language.Parser]::ParseFile($foundPatch, [ref]$toks, [ref]$errs) | Out-Null
    if ($errs -and $errs.Count -gt 0) {
        Write-ArgonError "The delivered patch file does not parse - it looks truncated or corrupt. Refusing to run it."
        $shown = 0
        foreach ($parseError in $errs) {
            if ($shown -ge 5) { break }
            $lineNumber = 0
            if ($parseError.Extent) { $lineNumber = $parseError.Extent.StartLineNumber }
            Write-Host ("      line {0}: {1}" -f $lineNumber, $parseError.Message) -ForegroundColor Red
            $shown++
        }
        if ($errs.Count -gt $shown) {
            Write-Host "      ... and $($errs.Count - $shown) more parse error(s)." -ForegroundColor Red
        }
        Write-Host ""
        Write-Host "  Download apply_changes.ps1 again - a truncated download is the usual cause." -ForegroundColor Gray
        exit 3
    }
    $rawPatchText = [System.IO.File]::ReadAllText($foundPatch, [System.Text.Encoding]::UTF8)
    $outsideHereStrings = [regex]::Replace($rawPatchText, "(?s)@'\r?\n.*?\r?\n'@|@`"\r?\n.*?\r?\n`"@", "@'`n'@")
    if ($outsideHereStrings -match '(?m)^\s*#{3,}\s' -or $outsideHereStrings.Contains('<ABSOLUTE_PROJECT_PATH>') -or $outsideHereStrings.Contains('<absolute target directory>')) {
        Write-ArgonError "The delivered patch file looks like markdown prose or an unrendered prompt template rather than a PowerShell script."
        exit 3
    }
    $hasActionableAst = $false
    if ($toks) {
        foreach ($tok in $toks) {
            $kind = $tok.Kind.ToString()
            if ($kind -eq 'Variable' -or $kind -eq 'Identifier' -or $kind -eq 'Generic') {
                $hasActionableAst = $true
                break
            }
        }
    }
    if (-not $hasActionableAst) {
        Write-ArgonError "The delivered patch file contains no executable PowerShell statements."
        exit 3
    }
    $nonEmptyLines = @($rawPatchText -split "\r?\n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })
    if ($nonEmptyLines.Count -eq 0) {
        Write-ArgonError "The delivered patch file contains no non-empty lines."
        exit 3
    }
    $lastNonEmptyLine = $nonEmptyLines[$nonEmptyLines.Count - 1]
    if (-not ($lastNonEmptyLine.StartsWith("#") -or $lastNonEmptyLine.EndsWith("}"))) {
        Write-ArgonError "The delivered patch file ends with a stray trailing line ('$lastNonEmptyLine'); expected a comment or closing brace."
        exit 3
    }
    Write-ArgonInfo "Patch file parsed successfully."

    # ------------------------------------------------------- dry run stops ----

    $targetPatch = Join-Path $projectRoot "apply_changes.ps1"

    if ($DryRun) {
        Write-ArgonWarn "Dry run - nothing was written and nothing was executed."
        Write-Host ""
        Write-Host "  Would copy   : $foundPatch" -ForegroundColor Gray
        Write-Host "                -> $targetPatch" -ForegroundColor Gray
        Write-Host "  Would run    : pwsh -NoProfile -ExecutionPolicy Bypass -File `"$targetPatch`"" -ForegroundColor White
        Write-Host "  Working dir  : $projectRoot" -ForegroundColor Gray
        Write-Host "  Rollback     : pwsh -NoProfile -ExecutionPolicy Bypass -File `"$applyScript`" -ProjectPath `"$projectRoot`" -Rollback" -ForegroundColor Gray
        Write-Host ""
        exit 0
    }

    # ------------------------------------------------ copy patch into place --

    if (Test-Path -LiteralPath $targetPatch -PathType Leaf) {
        $stamp = (Get-Date).ToString("yyyyMMdd_HHmmss")
        $backupCopy = "$targetPatch.bak-$stamp"
        [System.IO.File]::Copy($targetPatch, $backupCopy, $true)
        Write-ArgonWarn "Existing apply_changes.ps1 backed up to: $backupCopy"
    }
    if (-not [string]::Equals($foundPatch, $targetPatch, [System.StringComparison]::OrdinalIgnoreCase)) {
        # The browser may still hold a handle on a freshly downloaded file, so
        # retry the copy for a few seconds before giving up.
        $copied = $false
        $copyError = $null
        for ($attempt = 1; $attempt -le 12; $attempt++) {
            try {
                [System.IO.File]::Copy($foundPatch, $targetPatch, $true)
                $copied = $true
                break
            } catch {
                $copyError = $_
                Start-Sleep -Milliseconds 500
            }
        }
        if (-not $copied) {
            Write-ArgonError "Could not copy the delivered patch into place: $copyError"
            Write-Host "  The file is probably still locked by the browser - close Gemini and retry." -ForegroundColor Gray
            exit 4
        }
        Write-ArgonInfo "Patch copied to: $targetPatch"
    }
    else {
        Write-ArgonInfo "Patch already in place: $targetPatch"
    }

    # ----------------------------------------------------------- run it ------

    $shellExe = $null
    $pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($pwshCmd) { $shellExe = $pwshCmd.Source }
    if (-not $shellExe) {
        $winCmd = Get-Command powershell -ErrorAction SilentlyContinue
        if ($winCmd) { $shellExe = $winCmd.Source }
    }
    if (-not $shellExe) {
        Write-ArgonError "Neither pwsh nor powershell could be found on PATH."
        exit 1
    }
    Write-ArgonInfo "Running patch with: $shellExe"

    # Snapshot eligible project files before running the patch so rollback works
    # even if the patch script itself did not write to .argon_backup.
    $preSnapshotDir = Join-Path ([System.IO.Path]::GetTempPath()) ("argon_pre_" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $preSnapshotDir -Force | Out-Null
    $preHashes = @{}
    $snapSkipDirs = @(".argon_backup", "downloads", "__pycache__", "node_modules", ".git", "venv", ".venv")
    try {
        $existingFiles = @(Get-ChildItem -LiteralPath $projectRoot -Recurse -File -Force -ErrorAction SilentlyContinue)
        foreach ($ef in $existingFiles) {
            if ([string]::Equals($ef.FullName, $targetPatch, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
            if ($ef.Name -like "apply_changes.ps1.bak-*") { continue }
            $relEf = $ef.FullName.Substring($projectRoot.Length).TrimStart('\', '/')
            $skipEf = $false
            foreach ($seg in ($relEf -split '[\\/]')) {
                if ($snapSkipDirs -contains $seg.ToLowerInvariant()) { $skipEf = $true; break }
            }
            if ($skipEf -or $ef.Length -gt 15728640) { continue }
            $preHashes[$relEf] = (Get-FileHash -LiteralPath $ef.FullName -Algorithm SHA256).Hash
            $snapDest = Join-Path $preSnapshotDir $relEf
            $snapParent = [System.IO.Path]::GetDirectoryName($snapDest)
            if ($snapParent -and -not (Test-Path -LiteralPath $snapParent)) {
                New-Item -ItemType Directory -Path $snapParent -Force | Out-Null
            }
            [System.IO.File]::Copy($ef.FullName, $snapDest, $true)
        }
    } catch {}

    $runArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $targetPatch)
    $previousEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $patchOutput = New-Object System.Collections.Generic.List[string]
    try {
        Push-Location -LiteralPath $projectRoot
        try {
            & $shellExe @runArgs 2>&1 | ForEach-Object {
                $line = $_.ToString()
                $patchOutput.Add($line)
                Write-Host ("    " + $line)
            }
            $patchExitCode = $LASTEXITCODE
        } finally {
            Pop-Location
        }
    } finally {
        $ErrorActionPreference = $previousEap
    }

    try {
        $modifiedRels = New-Object System.Collections.Generic.List[string]
        foreach ($relEf in $preHashes.Keys) {
            $curPath = Join-Path $projectRoot $relEf
            if (-not (Test-Path -LiteralPath $curPath -PathType Leaf)) {
                $modifiedRels.Add($relEf)
            } else {
                $curHash = (Get-FileHash -LiteralPath $curPath -Algorithm SHA256).Hash
                if ($curHash -ne $preHashes[$relEf]) {
                    $modifiedRels.Add($relEf)
                }
            }
        }
        if ($modifiedRels.Count -gt 0) {
            $backupRoot = Join-Path $projectRoot ".argon_backup"
            New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
            $latestSession = Get-ChildItem -LiteralPath $backupRoot -Directory -ErrorAction SilentlyContinue |
                Sort-Object CreationTime, Name -Descending | Select-Object -First 1
            if (-not $latestSession -or $latestSession.CreationTime -lt (Get-Date).AddSeconds(-30)) {
                $sessionDir = Join-Path $backupRoot (Get-Date -Format "yyyyMMdd_HHmmss")
                New-Item -ItemType Directory -Path $sessionDir -Force | Out-Null
            } else {
                $sessionDir = $latestSession.FullName
            }
            foreach ($relEf in $modifiedRels) {
                $bkFile = Join-Path $sessionDir $relEf
                $snapFile = Join-Path $preSnapshotDir $relEf
                if (-not (Test-Path -LiteralPath $bkFile) -and (Test-Path -LiteralPath $snapFile)) {
                    $bkParent = [System.IO.Path]::GetDirectoryName($bkFile)
                    if ($bkParent -and -not (Test-Path -LiteralPath $bkParent)) {
                        New-Item -ItemType Directory -Path $bkParent -Force | Out-Null
                    }
                    [System.IO.File]::Copy($snapFile, $bkFile, $true)
                }
            }
        }
    } catch {} finally {
        if (Test-Path -LiteralPath $preSnapshotDir) {
            Remove-Item -LiteralPath $preSnapshotDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    if ($null -eq $patchExitCode) { $patchExitCode = 0 }

    Write-Host ""
    if ($patchExitCode -ne 0) {
        # A patch whose target blocks are missing was ALREADY applied - the
        # blocks are gone because they were replaced earlier. That is a
        # leftover delivery, not a broken patch, and it must be reported as
        # such instead of as a cryptic failure.
        $alreadyApplied = @($patchOutput | Where-Object { $_ -like "*Target block not found*" }).Count -gt 0
        if ($alreadyApplied) {
            Write-ArgonWarn "This patch has ALREADY been applied to $projectRoot."
            Write-Host ""
            Write-Host "  The target blocks are gone because they were replaced earlier, so" -ForegroundColor Gray
            Write-Host "  nothing was written this time. This is a leftover delivery, not a new one." -ForegroundColor Gray
            Write-Host "  Ask Gemini for a fresh delivery, or run a new task." -ForegroundColor Gray
            Write-Host ""
            Write-Host "  Patch file that was used: $foundPatch" -ForegroundColor DarkGray
            Write-Host ""
            exit 6
        }
        Write-ArgonError "Patch script failed with exit code $patchExitCode."
        Write-Host ""
        Write-Host "  To undo the changes, run:" -ForegroundColor Yellow
        Write-Host "    pwsh -NoProfile -ExecutionPolicy Bypass -File `"$applyScript`" -ProjectPath `"$projectRoot`" -Rollback" -ForegroundColor White
        Write-Host ""
        exit $patchExitCode
    }

    Write-ArgonInfo "Patch script finished successfully (exit code 0)."

    # Save any trailing # --- NOTES: ... # --- END NOTES --- block to its own file
    try {
        $openHs = [string][char]64 + [string][char]39
        $closeHs = [string][char]39 + [string][char]64
        $hsPattern = "(?s)" + [regex]::Escape($openHs) + "\r?\n.*?\r?\n" + [regex]::Escape($closeHs)
        $strippedForNotes = [regex]::Replace($rawPatchText, $hsPattern, "")
        $noteMatches = [regex]::Matches($strippedForNotes, '(?ms)^[ \t]*#\s*---\s*NOTES\b.*?^[ \t]*#\s*---\s*END NOTES\s*---')
        if ($noteMatches.Count -gt 0) {
            $lastNoteBlock = $noteMatches[$noteMatches.Count - 1].Value.Trim()
            $backupDirForNotes = Join-Path $projectRoot ".argon_backup"
            New-Item -ItemType Directory -Path $backupDirForNotes -Force | Out-Null
            $notesFile = Join-Path $backupDirForNotes ("argon_notes_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".md")
            $notesHeader = ('#' * 1) + " Argon notes and suggestions`n`nRecorded by Gemini 4 Argon while producing the patch.`n`n"
            [System.IO.File]::WriteAllText($notesFile, $notesHeader + $lastNoteBlock + "`n", [System.Text.UTF8Encoding]::new($false))
            Write-ArgonInfo "Saved Argon notes to: $notesFile"
        }
    } catch {}

    # --------------------------------------------------- what changed --------

    $cutoff = (Get-Date).AddMinutes(-15)
    $skipDirs = @(".argon_backup", "__pycache__", "node_modules", ".git")
    $recent = @()
    try {
        $recent = @(Get-ChildItem -LiteralPath $projectRoot -Recurse -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -ge $cutoff })
    } catch {
        $recent = @()
    }

    $changed = @()
    foreach ($file in $recent) {
        if ([string]::Equals($file.FullName, $targetPatch, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        $relative = $file.FullName.Substring($projectRoot.Length).TrimStart('\', '/')
        $blocked = $false
        foreach ($segment in ($relative -split '[\\/]')) {
            if ($skipDirs -contains $segment.ToLower()) { $blocked = $true; break }
        }
        if ($blocked) { continue }
        $changed += $relative
    }

    Write-Host ""
    Write-Host "Files changed in the last 15 minutes:" -ForegroundColor Green
    if ($changed.Count -eq 0) {
        Write-Host "  (none detected)" -ForegroundColor DarkGray
    } else {
        foreach ($relative in $changed) {
            Write-Host "  - $relative" -ForegroundColor Green
        }
    }
    Write-Host ""
    Write-Host "To undo these changes, run:" -ForegroundColor Yellow
    Write-Host "  pwsh -NoProfile -ExecutionPolicy Bypass -File `"$applyScript`" -ProjectPath `"$projectRoot`" -Rollback" -ForegroundColor White
    Write-Host ""
    exit 0
}
catch {
    Write-ArgonError "Unexpected failure: $($_.Exception.Message)"
    exit 1
}
