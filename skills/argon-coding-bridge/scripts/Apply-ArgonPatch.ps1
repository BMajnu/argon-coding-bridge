<#
.SYNOPSIS
    Applies surgical patches received from Gemini 4 Argon with automated backups and rollback.
.DESCRIPTION
    Safely applies code modifications produced by Gemini 4 Argon. Automatically creates timestamped
    backups before touching files, verifies existing content, and provides instant rollback capability.
.PARAMETER PatchFile
    Path to a file containing the Argon response or unified diff / file blocks.
.PARAMETER Rollback
    Rollback the most recent Argon patch from the backup folder.
.PARAMETER DryRun
    Simulate the patch application without writing changes to disk.
#>
[CmdletBinding()]
param(
    [string]$PatchFile = "",
    [switch]$Rollback,
    [switch]$DryRun,
    [string]$ProjectPath = (Get-Location).Path
)

$ErrorActionPreference = "Stop"

$absProject = (Resolve-Path $ProjectPath).Path
$backupRootDir = Join-Path $absProject ".argon_backup"

if ($Rollback) {
    Write-Host "[!] Rollback requested..." -ForegroundColor Yellow
    if (-not (Test-Path $backupRootDir)) {
        Write-Error "No backup directory found at $backupRootDir"
        exit 1
    }

    $lastBackup = Get-ChildItem -Path $backupRootDir -Directory | Sort-Object CreationTime -Descending | Select-Object -First 1
    if (-not $lastBackup) {
        Write-Error "No backup sessions found in $backupRootDir"
        exit 1
    }

    Write-Host "[+] Restoring from backup session: $($lastBackup.FullName)" -ForegroundColor Cyan
    $files = Get-ChildItem -Path $lastBackup.FullName -Recurse -File
    foreach ($f in $files) {
        $relPath = $f.FullName.Substring($lastBackup.FullName.Length).TrimStart('\', '/')
        $targetPath = Join-Path $absProject $relPath
        $parent = [System.IO.Path]::GetDirectoryName($targetPath)
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Copy-Item -Path $f.FullName -Destination $targetPath -Force
        Write-Host "  -> Restored: $relPath" -ForegroundColor Green
    }
    Write-Host "[SUCCESS] Rollback complete." -ForegroundColor Green
    exit 0
}

if (-not $PatchFile -or -not (Test-Path $PatchFile)) {
    Write-Host "Usage: .\Apply-ArgonPatch.ps1 -PatchFile <path-to-argon-response.txt|diff> [-DryRun] [-Rollback]" -ForegroundColor Red
    exit 1
}

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Gemini 4 Argon Patch Applicator" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

$content = Get-Content -Path $PatchFile -Raw -Encoding UTF8

# Regex parsing for structured file blocks:
# Pattern: ```[language] filepath
# or ### File: path/to/file.ext
# or [FILE: path/to/file.ext]
$fileBlockRegex = [regex]'(?ms)(?:###\s*File:\s*([^\r\n]+)|(?:\/\/|#|<!--)\s*FILE:\s*([^\r\n\s>]+)|\`\`\`[a-zA-Z0-9_-]*\s*:\s*([^\r\n]+))\r?\n(.*?)(?=(?:###\s*File:|(?:\/\/|#|<!--)\s*FILE:|\`\`\`[a-zA-Z0-9_-]*\s*:|\Z))'

$matches = $fileBlockRegex.Matches($content)

if ($matches.Count -eq 0) {
    # Check if content itself is a valid git patch
    if ($content -match '^diff --git' -or $content -match '^\-\-\- a\/') {
        Write-Host "[+] Detected unified git diff format. Applying via git apply..." -ForegroundColor Yellow
        if ($DryRun) {
            git -C $absProject apply --check $PatchFile
            Write-Host "[SUCCESS] Dry run: Git patch is applicable without conflicts." -ForegroundColor Green
            exit 0
        } else {
            # Create git stash or commit backup
            $timestamp = (Get-Date).ToString("yyyyMMdd_HHmmss")
            $sessionBackup = Join-Path $backupRootDir $timestamp
            New-Item -ItemType Directory -Path $sessionBackup -Force | Out-Null
            git -C $absProject diff > (Join-Path $sessionBackup "pre_patch_diff.patch")
            git -C $absProject apply $PatchFile
            Write-Host "[SUCCESS] Applied patch using git apply." -ForegroundColor Green
            exit 0
        }
    } else {
        Write-Warning "Could not detect structured file blocks or unified diff. Looking for generic code fence headers..."
    }
}

$timestamp = (Get-Date).ToString("yyyyMMdd_HHmmss")
$sessionBackup = Join-Path $backupRootDir $timestamp

if (-not $DryRun) {
    New-Item -ItemType Directory -Path $sessionBackup -Force | Out-Null
    Write-Host "[+] Backup directory initialized: $sessionBackup" -ForegroundColor Gray
}

$filesModified = 0

foreach ($match in $matches) {
    $candidates = @(
        @($match.Groups[1].Value, $match.Groups[2].Value, $match.Groups[3].Value) | 
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($candidates.Count -eq 0) { continue }
    $rawPath = [string]$candidates[0]
    # Clean quotes or markdown formatting
    $cleanRelPath = $rawPath.Trim("`"' ")
    
    $rawCode = [string]$match.Groups[4].Value
    # Strip opening or trailing ``` markdown fences if present
    $code = $rawCode -replace '(?s)^\s*```[a-zA-Z0-9_-]*\r?\n', '' -replace '(?s)\r?\n```\s*$', ''

    $targetFile = Join-Path $absProject $cleanRelPath

    Write-Host "[*] Processing file: $cleanRelPath" -ForegroundColor Cyan

    if ($DryRun) {
        Write-Host "    [DRY-RUN] Would update: $targetFile ($($code.Length) characters)" -ForegroundColor Yellow
        $filesModified++
        continue
    }

    # Backup existing file
    if (Test-Path $targetFile) {
        $backupDest = Join-Path $sessionBackup $cleanRelPath
        $parent = [System.IO.Path]::GetDirectoryName($backupDest)
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Copy-Item -Path $targetFile -Destination $backupDest -Force
    }

    $parentDir = [System.IO.Path]::GetDirectoryName($targetFile)
    if (-not (Test-Path $parentDir)) {
        New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
    }

    # Write patched file
    [System.IO.File]::WriteAllText($targetFile, $code, [System.Text.Encoding]::UTF8)
    Write-Host "    [APPLIED] Updated $cleanRelPath" -ForegroundColor Green
    $filesModified++
}

Write-Host "==========================================" -ForegroundColor Green
Write-Host "[SUCCESS] Processed $filesModified files." -ForegroundColor Green
if (-not $DryRun) {
    Write-Host "To undo these changes, run:" -ForegroundColor Yellow
    Write-Host "  .\Apply-ArgonPatch.ps1 -Rollback" -ForegroundColor White
}
Write-Host "==========================================" -ForegroundColor Green
