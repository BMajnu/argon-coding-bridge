<#
.SYNOPSIS
    Packages codebase context cleanly for Gemini 4 Argon, strictly respecting .gitignore and dependency exclusions.
.DESCRIPTION
    Creates a clean zip archive containing only true project source code.
    Strictly excludes third-party packages (node_modules, vendor, Pods, .gradle, .dart_tool, venv),
    build artifacts (dist, build, .next, target, bin, obj), binary media, and secrets (.env*).
    Enforces a realistic max code file size (default 15MB) to keep packaging robust.
.PARAMETER ProjectPath
    The root path of the project. Defaults to current directory.
.PARAMETER TargetFiles
    Optional specific files or comma-separated list of files to include instead of entire project.
.PARAMETER OutputZip
    Output path for the generated zip archive. Defaults to ./argon_context.zip.
.PARAMETER TaskDescription
    Description of the task/bug/feature to embed into the package.
.PARAMETER MaxFileSizeMB
    Maximum allowable size for a single code/data file in MB. Defaults to 15MB.
#>
[CmdletBinding()]
param(
    [string]$ProjectPath = (Get-Location).Path,
    [string[]]$TargetFiles = @(),
    [string]$OutputZip = "argon_context.zip",
    [string]$TaskDescription = "",
    [int]$MaxFileSizeMB = 15
)

$ErrorActionPreference = "Stop"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Gemini 4 Argon Context Packager" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

$absProjectPath = (Resolve-Path $ProjectPath).Path
Write-Host "[+] Source Directory: $absProjectPath" -ForegroundColor Gray
Write-Host "[+] Max Source File Size: $MaxFileSizeMB MB" -ForegroundColor Gray

if (-not (Test-Path $absProjectPath)) {
    Write-Error "Project path does not exist: $absProjectPath"
    exit 1
}

$maxBytes = $MaxFileSizeMB * 1024 * 1024

# Comprehensive blacklist for package managers, build systems, caches, and IDEs
$dependencyAndBuildDirs = @(
    # VCS & IDEs
    ".git", ".svn", ".hg", ".idea", ".vscode",
    # JavaScript / Node / Bun / Deno
    "node_modules", ".next", ".nuxt", ".turbo", ".svelte-kit", ".output",
    ".npm", ".yarn", ".pnpm-store", "bower_components",
    # Python
    "venv", ".venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache", ".tox", "site-packages",
    # Flutter / Dart
    ".dart_tool", ".pub-cache", ".pub",
    # Mobile / iOS / Android
    "Pods", "DerivedData", ".gradle", ".expo",
    # Java / Kotlin / Rust / Go / .NET
    "target", "bin", "obj", "packages", ".m2", "pkg",
    # General build outputs & test coverage
    "dist", "build", "out", "coverage", ".cache", "logs"
)

# Files that should never be in the zip (binaries, archives, lock dumps)
$ignoreExtensions = @(
    ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
    ".pdf", ".mp4", ".mkv", ".mov", ".avi", ".mp3", ".wav",
    ".iso", ".bin", ".pyc", ".class", ".apk", ".aab", ".ipa",
    ".sqlite", ".sqlite3", ".db"
)

# Security & Secrets protection
$secretPatterns = @(
    "^\.env.*", ".*id_rsa.*", ".*\.pem$", ".*\.key$", ".*\.keystore$", ".*credentials.*\.json$"
)

# Function to check if a relative path contains blacklisted dependency/build folders
function Test-IsDependencyOrBuildDir([string]$relativePath) {
    $segments = $relativePath.Split([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    foreach ($seg in $segments) {
        if ($dependencyAndBuildDirs -contains $seg.ToLower()) {
            return $true
        }
    }
    return $false
}

# Function to check if a file name represents sensitive secrets
function Test-IsSecret([string]$fileName) {
    foreach ($pat in $secretPatterns) {
        if ($fileName -match $pat) { return $true }
    }
    return $false
}

# Collect files
$selectedFiles = [System.Collections.Generic.List[string]]::new()

$hasGit = $false
try {
    $gitCheck = git -C $absProjectPath rev-parse --is-inside-work-tree 2>$null
    if ($LASTEXITCODE -eq 0 -and $gitCheck -eq "true") {
        $hasGit = $true
    }
} catch {
    $hasGit = $false
}

if ($TargetFiles.Count -gt 0) {
    Write-Host "[+] Packaging targeted files explicitly requested..." -ForegroundColor Yellow
    foreach ($pattern in $TargetFiles) {
        $matching = Get-ChildItem -Path $absProjectPath -Filter $pattern -Recurse -File -ErrorAction SilentlyContinue
        foreach ($f in $matching) {
            $rel = $f.FullName.Substring($absProjectPath.Length).TrimStart('\', '/')
            if (-not (Test-IsDependencyOrBuildDir $rel) -and -not (Test-IsSecret $f.Name)) {
                if (-not $selectedFiles.Contains($rel)) {
                    $selectedFiles.Add($rel)
                }
            }
        }
    }
} elseif ($hasGit) {
    Write-Host "[+] Using Git .gitignore rules (git ls-files --cached --others --exclude-standard)..." -ForegroundColor Green
    $gitFiles = git -C $absProjectPath ls-files -c -o --exclude-standard
    foreach ($file in $gitFiles) {
        # Force strict check against dependencies even if accidentally committed into git
        if (Test-IsDependencyOrBuildDir $file) { continue }

        $fullPath = Join-Path $absProjectPath $file
        $ext = [System.IO.Path]::GetExtension($file).ToLower()
        $base = [System.IO.Path]::GetFileName($file)

        if (Test-IsSecret $base) { continue }
        if ($ignoreExtensions -contains $ext) { continue }

        if (Test-Path $fullPath -PathType Leaf) {
            $item = Get-Item $fullPath
            if ($item.Length -gt $maxBytes) {
                Write-Host "  [-] Skipping oversized file (> $MaxFileSizeMB MB): $file ($([math]::Round($item.Length / 1MB, 2)) MB)" -ForegroundColor DarkYellow
                continue
            }
            $selectedFiles.Add($file.Replace('/', '\'))
        }
    }
} else {
    Write-Host "[+] Git not detected. Reading local .gitignore and applying dependency blacklist..." -ForegroundColor Yellow
    
    # Read custom .gitignore if present
    $customIgnores = @()
    $gitignorePath = Join-Path $absProjectPath ".gitignore"
    if (Test-Path $gitignorePath) {
        $lines = Get-Content -Path $gitignorePath -Encoding UTF8
        foreach ($line in $lines) {
            $trimmed = $line.Trim()
            if ($trimmed -and -not $trimmed.StartsWith("#")) {
                $customIgnores += $trimmed.TrimStart('/')
            }
        }
    }

    $allFiles = Get-ChildItem -Path $absProjectPath -Recurse -File -Force -ErrorAction SilentlyContinue
    foreach ($f in $allFiles) {
        $rel = $f.FullName.Substring($absProjectPath.Length).TrimStart('\', '/')
        
        # Check hardcoded dependency/build blacklist
        if (Test-IsDependencyOrBuildDir $rel) { continue }

        # Check custom gitignore rules
        $skipCustom = $false
        foreach ($ign in $customIgnores) {
            if ($rel -like "*$ign*" -or $f.Name -like $ign) {
                $skipCustom = $true
                break
            }
        }
        if ($skipCustom) { continue }

        # Check secrets
        if (Test-IsSecret $f.Name) { continue }

        # Check extensions
        $ext = $f.Extension.ToLower()
        if ($ignoreExtensions -contains $ext) { continue }

        # Check max size limit
        if ($f.Length -gt $maxBytes) {
            Write-Host "  [-] Skipping oversized file (> $MaxFileSizeMB MB): $rel ($([math]::Round($f.Length / 1MB, 2)) MB)" -ForegroundColor DarkYellow
            continue
        }

        $selectedFiles.Add($rel)
    }
}

Write-Host "[+] Total eligible codebase files: $($selectedFiles.Count)" -ForegroundColor Green

# Prepare staging temporary directory
$stagingDir = Join-Path ([System.IO.Path]::GetTempPath()) ("argon_pkg_" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $stagingDir -Force | Out-Null

try {
    foreach ($rel in $selectedFiles) {
        $src = Join-Path $absProjectPath $rel
        $dst = Join-Path $stagingDir $rel
        $parent = [System.IO.Path]::GetDirectoryName($dst)
        if (-not (Test-Path $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        Copy-Item -Path $src -Destination $dst -Force
    }

    # Generate TASK_BRIEF.md inside the archive
    $briefPath = Join-Path $stagingDir "TASK_BRIEF.md"
    $timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $briefContent = @"
# Task Brief for Gemini 4 Argon
**Generated:** $timestamp
**Project Root:** $([System.IO.Path]::GetFileName($absProjectPath))
**Included Files Count:** $($selectedFiles.Count)

## User Goal / Problem Statement:
$TaskDescription

---

## CRITICAL INSTRUCTIONS FOR GEMINI 4 ARGON:
1. **SURGICAL EDITS ONLY (ZERO UI REGRESSION):**
   - Do NOT rewrite or restructure entire files unless you are creating a completely new file.
   - Do NOT modify existing UI layouts, component hierarchies, CSS/Tailwind classes, or visual styling unless explicitly requested in the task goal above.
   - For edits in existing files, use safe substring replacements (`$content.Replace(...)`) with an error throw if not found.
   - Use single-quoted Here-Strings (`@' ... '@`) so JavaScript/TypeScript `$``{var}` template literals are not corrupted.
   - Write files with UTF-8 without BOM: `[System.Text.UTF8Encoding]::new($false)`.
"@
    Set-Content -Path $briefPath -Value $briefContent -Encoding UTF8

    # Final Zip output path
    $resolvedZip = if ([System.IO.Path]::IsPathRooted($OutputZip)) {
        $OutputZip
    } else {
        Join-Path $absProjectPath $OutputZip
    }

    if (Test-Path $resolvedZip) {
        Remove-Item $resolvedZip -Force
    }

    Write-Host "[+] Compressing clean archive to: $resolvedZip" -ForegroundColor Yellow
    [System.IO.Compression.ZipFile]::CreateFromDirectory($stagingDir, $resolvedZip, [System.IO.Compression.CompressionLevel]::Optimal, $false)

    $zipItem = Get-Item $resolvedZip
    $zipSizeKB = [math]::Round($zipItem.Length / 1KB, 2)
    Write-Host "==========================================" -ForegroundColor Green
    Write-Host "[SUCCESS] Zip Package Ready (100% Clean Context)!" -ForegroundColor Green
    Write-Host "Path: $resolvedZip" -ForegroundColor White
    Write-Host "Size: $zipSizeKB KB ($($selectedFiles.Count) files)" -ForegroundColor White
    Write-Host "==========================================" -ForegroundColor Green
}
finally {
    if (Test-Path $stagingDir) {
        Remove-Item -Path $stagingDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
