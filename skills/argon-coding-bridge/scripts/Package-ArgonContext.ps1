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
    [int]$MaxFileSizeMB = 50
)

$ErrorActionPreference = "Stop"

if (-not ("System.IO.Compression.ZipFile" -as [type])) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
}

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Gemini 4 Argon Context Packager" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

$absProjectPath = (Get-Item -LiteralPath (Resolve-Path -LiteralPath $ProjectPath).Path).FullName
Write-Host "[+] Source Directory: $absProjectPath" -ForegroundColor Gray
Write-Host "[+] Max Source File Size: $MaxFileSizeMB MB" -ForegroundColor Gray

if (-not (Test-Path $absProjectPath)) {
    Write-Error "Project path does not exist: $absProjectPath"
    exit 1
}

$maxBytes = $MaxFileSizeMB * 1024 * 1024

# Unambiguous dependency, build-output, and cache directories only.
# Never drop packages, bin, out, build, pkg, obj, or logs globally.
$dependencyAndBuildDirs = @(
    ".git",
    "node_modules",
    ".next",
    "venv",
    ".venv",
    "__pycache__",
    "site-packages",
    ".dart_tool",
    ".pub-cache",
    "pods",
    "deriveddata",
    ".gradle",
    "target",
    "dist",
    "coverage",
    ".cache"
)

$ambiguousDirs = @(
    "packages", "bin", "out", "build", "pkg", "obj", "logs",
    ".svn", ".hg", ".idea", ".vscode", ".nuxt", ".turbo", ".svelte-kit",
    ".output", "env", ".m2", ".expo"
)

$sourceExtensions = @(
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java", ".kt",
    ".cs", ".cpp", ".c", ".h", ".hpp", ".swift", ".dart", ".rb", ".php",
    ".vue", ".svelte", ".json", ".yaml", ".yml", ".toml", ".md", ".sql",
    ".sh", ".ps1", ".bat", ".cmd", ".html", ".css", ".scss", ".less",
    ".xml", ".proto", ".graphql", ".gql"
)

$ignoreExtensions = @(
    ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
    ".pdf", ".mp4", ".mkv", ".mov", ".avi", ".mp3", ".wav",
    ".iso", ".bin", ".pyc", ".class", ".apk", ".aab", ".ipa",
    ".sqlite", ".sqlite3", ".db", ".o", ".obj", ".pdb", ".lib", ".a"
)

# Real key and credential patterns only (never drop arbitrary .json files just
# because the substring 'secret' appears in the name).
$secretPatterns = @(
    "^\.env.*",
    ".*id_rsa.*",
    ".*id_ed25519.*",
    ".*id_ecdsa.*",
    ".*id_dsa.*",
    ".*\.pem$",
    ".*\.key$",
    ".*\.keystore$",
    ".*\.pfx$",
    ".*\.p12$",
    "^(?:google_?|gcp_?|aws_?|firebase_?|oauth_?|service[_-]account[_-]?)?credentials(?:[_.-].*)?\.json$",
    "^client_secrets?(?:[_.-].*)?\.json$",
    "^(?:secrets?|passwords?|service[_-]account)\.json$",
    "^\.(?:netrc|npmrc|pypirc)$",
    ".*accounts?[_-]?pool.*"
)

function Test-IsDependencyOrBuildDir([string]$relativePath) {
    $segments = $relativePath.Split([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    foreach ($seg in $segments) {
        if ($dependencyAndBuildDirs -contains $seg.ToLowerInvariant()) {
            return $true
        }
    }
    return $false
}

function Test-IsInAmbiguousDir([string]$relativePath) {
    $segments = $relativePath.Split([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    if ($segments.Length -le 1) { return $false }
    for ($i = 0; $i -lt ($segments.Length - 1); $i++) {
        if ($ambiguousDirs -contains $segments[$i].ToLowerInvariant()) {
            return $true
        }
    }
    return $false
}

function Test-IsSourceFile([System.IO.FileInfo]$fileItem) {
    $ext = $fileItem.Extension.ToLowerInvariant()
    if ($sourceExtensions -contains $ext) { return $true }
    if (@("Dockerfile", "Makefile", "LICENSE", "README", ".gitignore") -contains $fileItem.Name) { return $true }
    return $false
}

function Test-IsSecret([string]$fileName) {
    $lower = $fileName.ToLowerInvariant()
    foreach ($pat in $secretPatterns) {
        if ($lower -match $pat) { return $true }
    }
    return $false
}

$generatedArtefacts = @(
    'apply_changes.ps1',
    'argon_context.zip',
    'argon_package.zip',
    'argon_task_prompt.md',
    'argon_response.txt',
    'argon_response.md',
    'task_brief.md',
    'api_index.md',
    'accounts_pool.json',
    'argon_sessions.json',
    'argon_task_state.json'
)
$generatedDirs = @('.argon_backup', 'downloads', '__pycache__')

function Test-IsGeneratedArtefact([string]$relPath) {
    $name = [System.IO.Path]::GetFileName($relPath).ToLowerInvariant()
    if ($generatedArtefacts -contains $name) { return $true }
    if ($name -like 'apply_changes*.ps1' -or $name -like 'apply_changes.ps1.bak-*') { return $true }
    if ($name -like '*accounts_pool*' -or $name -like '*account_pool*' -or $name -like '*account-pool*') { return $true }
    $normRel = ($relPath -replace '\\', '/').ToLowerInvariant().TrimStart('/')
    if ($normRel -eq 'skills/argon-coding-bridge/config/accounts_pool.json' -or $normRel.EndsWith('/config/accounts_pool.json')) {
        return $true
    }
    foreach ($seg in ($relPath -split '[\\/]')) {
        if ($generatedDirs -contains $seg.ToLowerInvariant()) { return $true }
    }
    return $false
}

function Format-ArgonFileSize([long]$bytes) {
    if ($bytes -ge 1048576) {
        return ("{0:N0} bytes ({1:N2} MB)" -f $bytes, ($bytes / 1MB))
    }
    if ($bytes -ge 1024) {
        return ("{0:N0} bytes ({1:N2} KB)" -f $bytes, ($bytes / 1KB))
    }
    return ("{0:N0} bytes" -f $bytes)
}

$declarationExtensions = @(
    ".rs", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".go", ".java", ".kt", ".kts", ".cs", ".swift", ".dart", ".c", ".cpp", ".h",
    ".hpp", ".rb", ".php", ".ex", ".exs", ".ps1", ".sh"
)

function Get-ArgonVerificationCommand([string]$ProjectRoot) {
    $cargoToml = Join-Path $ProjectRoot "Cargo.toml"
    if (Test-Path -LiteralPath $cargoToml -PathType Leaf) {
        $hasClippy = $null -ne (Get-Command "cargo-clippy" -ErrorAction SilentlyContinue)
        if ($hasClippy) {
            return "cargo check && cargo clippy --all-targets"
        }
        return "cargo check"
    }

    $pkgJson = Join-Path $ProjectRoot "package.json"
    if (Test-Path -LiteralPath $pkgJson -PathType Leaf) {
        $hasLint = $false
        $hasTest = $false
        try {
            $pkgObj = Get-Content -LiteralPath $pkgJson -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($pkgObj -and $pkgObj.scripts) {
                if ($pkgObj.scripts.PSObject.Properties.Name -contains "lint" -and -not [string]::IsNullOrWhiteSpace([string]$pkgObj.scripts.lint)) {
                    $hasLint = $true
                }
                if ($pkgObj.scripts.PSObject.Properties.Name -contains "test") {
                    $tVal = [string]$pkgObj.scripts.test
                    if (-not [string]::IsNullOrWhiteSpace($tVal) -and ($tVal.ToLowerInvariant() -notlike "*no test specified*")) {
                        $hasTest = $true
                    }
                }
            }
        } catch {}
        $cmds = @()
        if ($hasLint) { $cmds += "npm run lint" } else { $cmds += "npx tsc --noEmit" }
        if ($hasTest) { $cmds += "npm test" }
        return ($cmds -join " && ")
    }

    $pyMarkers = @("pyproject.toml", "setup.cfg", "tox.ini") | ForEach-Object { Join-Path $ProjectRoot $_ } | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
    if (@($pyMarkers).Count -gt 0) {
        $combined = ""
        foreach ($pm in $pyMarkers) {
            try { $combined += "`n" + ([System.IO.File]::ReadAllText($pm, [System.Text.Encoding]::UTF8)).ToLowerInvariant() } catch {}
        }
        $hasRuff = $combined.Contains("[tool.ruff") -or $combined.Contains("[ruff") -or (Test-Path -LiteralPath (Join-Path $ProjectRoot "ruff.toml") -PathType Leaf) -or (Test-Path -LiteralPath (Join-Path $ProjectRoot ".ruff.toml") -PathType Leaf)
        $cmds = @()
        if ($hasRuff) { $cmds += "ruff check ." } else { $cmds += "python -m compileall -q ." }
        $hasPytest = $combined.Contains("pytest") -or (Test-Path -LiteralPath (Join-Path $ProjectRoot "pytest.ini") -PathType Leaf) -or (Test-Path -LiteralPath (Join-Path $ProjectRoot "conftest.py") -PathType Leaf)
        if ($hasPytest) { $cmds += "pytest -q" }
        return ($cmds -join " && ")
    }

    if (Test-Path -LiteralPath (Join-Path $ProjectRoot "go.mod") -PathType Leaf) {
        return "go build ./... && go vet ./..."
    }
    if (Test-Path -LiteralPath (Join-Path $ProjectRoot "pom.xml") -PathType Leaf) {
        return "mvn -q -DskipTests compile"
    }
    $gradleKts = Join-Path $ProjectRoot "build.gradle.kts"
    $gradleGroovy = Join-Path $ProjectRoot "build.gradle"
    if ((Test-Path -LiteralPath $gradleKts -PathType Leaf) -or (Test-Path -LiteralPath $gradleGroovy -PathType Leaf)) {
        $isKt = (Test-Path -LiteralPath $gradleKts -PathType Leaf) -or (Test-Path -LiteralPath (Join-Path $ProjectRoot "src\main\kotlin") -PathType Container)
        if ($isKt) { return "./gradlew compileKotlin" }
        return "./gradlew compileJava"
    }
    $slnOrCsproj = @(Get-ChildItem -LiteralPath $ProjectRoot -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -in @(".sln", ".csproj") })
    if ($slnOrCsproj.Count -gt 0) {
        return "dotnet build"
    }
    $makefile = Join-Path $ProjectRoot "Makefile"
    if (Test-Path -LiteralPath $makefile -PathType Leaf) {
        $mkText = [System.IO.File]::ReadAllText($makefile, [System.Text.Encoding]::UTF8)
        $cmds = @("make")
        if ($mkText -match '(?m)^test\s*:(?!=)') { $cmds += "make test" }
        return ($cmds -join " && ")
    }
    if (Test-Path -LiteralPath (Join-Path $ProjectRoot "mix.exs") -PathType Leaf) {
        return "mix compile --warnings-as-errors"
    }
    $gemfile = Join-Path $ProjectRoot "Gemfile"
    if (Test-Path -LiteralPath $gemfile -PathType Leaf) {
        $gemText = ([System.IO.File]::ReadAllText($gemfile, [System.Text.Encoding]::UTF8)).ToLowerInvariant()
        if ($gemText.Contains("rubocop") -or (Test-Path -LiteralPath (Join-Path $ProjectRoot ".rubocop.yml") -PathType Leaf)) {
            return "bundle exec rubocop"
        }
    }
    return ""
}

function Test-ArgonDeclarationLine([string]$Ext, [string]$RawLine, [string]$Stripped) {
    if ([string]::IsNullOrWhiteSpace($Stripped)) { return $false }
    if ($Stripped.StartsWith("//") -or $Stripped.StartsWith("/*") -or $Stripped.StartsWith("*") -or $Stripped.StartsWith("<!--")) { return $false }
    if (@(".py", ".ps1", ".sh", ".rb", ".ex", ".exs") -contains $Ext -and $Stripped.StartsWith("#")) { return $false }

    if ($Ext -eq ".rs") {
        return ($Stripped -match '^(?:pub(?:\s*\([^)]*\))?\s+)?(?:(?:async|unsafe|const|default|extern(?:\s+"[^"]*")?)\s+)*(?:fn|struct|enum|trait|type|const|static|mod|macro_rules!)\s+[A-Za-z_]\w*|^impl(?:\s*<[^>]*>)?\s+[A-Za-z_:<>\w\s,''&]+')
    }
    if (@(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte") -contains $Ext) {
        return ($Stripped -match '^(?:export\s+(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?(?:function|const|let|var|class|interface|type|enum)\b|export\s+default\b|(?:async\s+)?function\s+[A-Za-z_$][\w$]*|class\s+[A-Za-z_$][\w$]*|(?:interface|type|enum)\s+[A-Za-z_$][\w$]*)')
    }
    if ($Ext -eq ".py") {
        if ($Stripped -match '^(?:async\s+def|def|class)\s+[A-Za-z_]\w*') { return $true }
        if ($RawLine -eq $RawLine.TrimStart() -and $Stripped -match '^[A-Z][A-Z0-9_]{2,}\s*(?::[^=\n]+)?=') { return $true }
        return $false
    }
    if ($Ext -eq ".go") {
        return ($Stripped -match '^(?:func|type|const|var)\s+(?:\([^)]+\)\s*)?[A-Za-z_(]')
    }
    if (@(".java", ".kt", ".kts", ".cs", ".swift", ".dart", ".c", ".cpp", ".h", ".hpp", ".php", ".rb", ".ex", ".exs") -contains $Ext) {
        return ($Stripped -match '^(?:(?:public|protected|internal|private|static|final|abstract|open|suspend|override|data|sealed|inline|extern|virtual|async|partial|readonly)\s+)*(?:class|interface|enum|record|struct|object|trait|protocol|extension|fun|func|def|defp|defmodule|val|var|void|[A-Za-z_][\w<>,.\[\]?]*\s+[A-Za-z_]\w*\s*\()')
    }
    if ($Ext -eq ".ps1") {
        return ($Stripped -match '^(?:function|filter)\s+[A-Za-z_][\w-]*|^param\s*\(')
    }
    if ($Ext -eq ".sh") {
        return ($Stripped -match '^(?:function\s+[A-Za-z_]\w*|[A-Za-z_]\w*\s*\(\s*\))')
    }
    return $false
}

function Get-ArgonFileDeclarations([string]$FullPath, [string]$RelPath) {
    $ext = [System.IO.Path]::GetExtension($RelPath).ToLowerInvariant()
    $results = [System.Collections.Generic.List[object]]::new()
    if ($declarationExtensions -notcontains $ext) { return $results }
    if (-not (Test-Path -LiteralPath $FullPath -PathType Leaf)) { return $results }
    try {
        $rawText = [System.IO.File]::ReadAllText($FullPath, [System.Text.Encoding]::UTF8)
    } catch {
        return $results
    }
    $fileLines = $rawText -split "\r?\n"
    $totalLines = $fileLines.Count
    for ($idx = 0; $idx -lt $totalLines; $idx++) {
        $rawLine = $fileLines[$idx]
        $stripped = $rawLine.Trim()
        if (-not (Test-ArgonDeclarationLine -Ext $ext -RawLine $rawLine -Stripped $stripped)) { continue }
        $sigParts = [System.Collections.Generic.List[string]]::new()
        $sigParts.Add($stripped)
        $isConstAssign = ($ext -eq ".py" -and $rawLine -eq $rawLine.TrimStart() -and $stripped -match '^[A-Z][A-Z0-9_]{2,}\s*(?::[^=\n]+)?=')
        if (-not $isConstAssign) {
            $openParens = ($stripped.Split("(").Length - 1) - ($stripped.Split(")").Length - 1)
            $lookahead = $idx + 1
            while ($openParens -gt 0 -and $lookahead -lt [math]::Min($totalLines, $idx + 20)) {
                $nxt = $fileLines[$lookahead].Trim()
                if ($nxt -and -not $nxt.StartsWith("#") -and -not $nxt.StartsWith("//")) {
                    $sigParts.Add($nxt)
                    $openParens += ($nxt.Split("(").Length - 1) - ($nxt.Split(")").Length - 1)
                }
                $lookahead++
            }
        }
        $sig = (($sigParts -join " ") -replace '\s+', ' ').Trim()
        $results.Add([PSCustomObject]@{ Line = ($idx + 1); Signature = $sig })
    }
    return $results
}

function Build-ArgonApiIndex([string]$ProjectRoot, $SelectedFiles) {
    $h1 = '#'
    $h2 = ('#' * 2)
    $bullet = [string][char]45
    $perFile = [System.Collections.Generic.List[object]]::new()
    $totalDecls = 0
    $scannedFiles = 0

    $sortedRels = @($SelectedFiles | Sort-Object { ($_ -replace '\\', '/').ToLowerInvariant() })
    foreach ($rel in $sortedRels) {
        $ext = [System.IO.Path]::GetExtension($rel).ToLowerInvariant()
        if ($declarationExtensions -notcontains $ext) { continue }
        $scannedFiles++
        $full = Join-Path $ProjectRoot $rel
        $decls = Get-ArgonFileDeclarations -FullPath $full -RelPath $rel
        $totalDecls += $decls.Count
        $relPosix = ($rel -replace '\\', '/')
        $perFile.Add([PSCustomObject]@{ RelPosix = $relPosix; Decls = $decls })
    }

    $out = [System.Collections.Generic.List[string]]::new()
    $out.Add("$h1 API_INDEX.md - Complete Public & Source Declaration Index")
    $out.Add("Project Root: $([System.IO.Path]::GetFileName($ProjectRoot))")
    $out.Add("Source Files Scanned: $scannedFiles")
    $out.Add("Total Declarations: $totalDecls (complete - no truncation)")
    $out.Add("")
    $out.Add("API_INDEX.md lists every public declaration with its file and line. Use THAT -")
    $out.Add("never invent a function name, a parameter or a return type. If a symbol you need")
    $out.Add("is not in API_INDEX.md and not in the archive, DO NOT GUESS: name the missing")
    $out.Add("symbol and stop.")
    $out.Add("")
    foreach ($item in $perFile) {
        $out.Add("$h2 $($item.RelPosix) ($($item.Decls.Count) declaration(s))")
        if ($item.Decls.Count -eq 0) {
            $out.Add("$bullet (no declarations matched)")
        } else {
            foreach ($d in $item.Decls) {
                $out.Add(("{0} {1}:{2} -> {3}" -f $bullet, $item.RelPosix, $d.Line, $d.Signature))
            }
        }
        $out.Add("")
    }
    return (($out -join "`n").TrimEnd() + "`n")
}

function Complete-ArgonTaskBrief([string]$BaseBrief, [string]$ProjectRoot, $SelectedFiles, $ExcludedEntries) {
    $h2 = ('#' * 2)
    $h3 = ('#' * 3)
    $bullet = [string][char]45

    $detectedCmd = Get-ArgonVerificationCommand -ProjectRoot $ProjectRoot
    $pyRels = @($SelectedFiles | Sort-Object | Where-Object { [System.IO.Path]::GetExtension($_).ToLowerInvariant() -eq ".py" } | ForEach-Object { ($_ -replace '\\', '/') })
    if ($detectedCmd) {
        $verifyBlock = @(
            "Detected command: ``$detectedCmd``",
            "If automatic detection is not sure, name the exact command that verifies this",
            "project under ``$h2 VERIFICATION COMMAND`` in your reply and notes so the bridge",
            "can run it directly."
        ) -join "`n"
    } elseif ($pyRels.Count -gt 0) {
        $fallbackPy = "python -m py_compile " + ($pyRels -join " ")
        $verifyBlock = @(
            "Detected root marker command: ``(none - no root build marker matched)``",
            "Archive verification command: ``$fallbackPy``",
            "Argon: confirm or name the exact command that verifies this project under",
            "``$h2 VERIFICATION COMMAND`` in your reply and notes so the bridge can read it",
            "straight from the archive when automatic root detection is not sure."
        ) -join "`n"
    } else {
        $verifyBlock = @(
            "Detected command: ``(none - automatic detection did not match a project root build marker)``",
            "Argon: name the exact command that verifies this project under",
            "``$h2 VERIFICATION COMMAND`` in your reply and notes so the bridge can read it",
            "straight from the archive when automatic detection is not sure."
        ) -join "`n"
    }
    $topLines = @(
        "$h2 FIND THE ROOT CAUSE FIRST",
        "",
        "If something in this project is broken, do not change anything until you can",
        "state the root cause and the EVIDENCE for it (the exact file and line, the",
        "exact DOM node you measured, the exact value you read).",
        "",
        "- Every symptom must be traced to a concrete cause in THIS code base.",
        "- If you cannot find the cause in the archive, say exactly what you need -",
        "  do not invent a fix and do not guess a signature, a path or a value.",
        "- Put the root cause and its evidence in the NOTES block of your patch, under",
        "  the heading ``$h2 ROOT CAUSE``.",
        "",
        "$h2 THE EXACT API OF THIS PROJECT",
        "",
        "API_INDEX.md lists every public declaration with its file and line. Use THAT -",
        "never invent a function name, a parameter or a return type. If a symbol you need",
        "is not in API_INDEX.md and not in the archive, DO NOT GUESS: name the missing",
        "symbol and stop.",
        "",
        "$h2 VERIFICATION COMMAND",
        "",
        $verifyBlock
    ) -join "`n"

    $critMarker = "$h2 CRITICAL INSTRUCTIONS FOR GEMINI 4 ARGON:"
    if ($BaseBrief.Contains($critMarker)) {
        $BaseBrief = $BaseBrief.Replace($critMarker, ($topLines + "`n`n" + $critMarker))
    } else {
        $BaseBrief = $BaseBrief.TrimEnd() + "`n`n" + $topLines
    }

    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add($BaseBrief.TrimEnd())
    $lines.Add("")
    $lines.Add("$h3 6. DO NOT GUESS MISSING CONTEXT & KEEP PAYLOADS BYTE-EXACT (CRITICAL)")
    $lines.Add("If something you need is not in this archive - a file, an API endpoint, a")
    $lines.Add("function, a configuration - DO NOT GUESS and DO NOT INVENT it. Say exactly")
    $lines.Add("which file or symbol is missing and stop there. A wrong guess is much worse")
    $lines.Add("than an incomplete answer.")
    $lines.Add("")
    $lines.Add("Inside a patch, a Rust (or any) char or string literal for a newline is the TWO")
    $lines.Add("characters backslash and n (\n) - never a real newline. The same applies to")
    $lines.Add("\t, \r, \\ and to every other escape. The patch must not contain a raw")
    $lines.Add("control character at all. A straight apostrophe ' must stay a straight")
    $lines.Add("apostrophe - never a typographic quote. Keep opening and closing braces")
    $lines.Add("balanced in every replacement block.")
    $lines.Add("")
    $lines.Add("$h2 COMPLETE PACKED FILE TREE ($($SelectedFiles.Count) files - no truncation)")
    $lines.Add("")
    $lines.Add("Every file packed into this archive is listed below with its exact size:")
    $lines.Add("")
    foreach ($rel in ($SelectedFiles | Sort-Object)) {
        $full = Join-Path $ProjectRoot $rel
        $sz = 0L
        if (Test-Path -LiteralPath $full -PathType Leaf) {
            $sz = (Get-Item -LiteralPath $full).Length
        }
        $relPosix = ($rel -replace '\\', '/')
        $lines.Add(("{0} {1,-56} ({2})" -f $bullet, $relPosix, (Format-ArgonFileSize $sz)))
    }
    $lines.Add("")
    $lines.Add("$h2 FILES THAT ARE NOT IN THIS ARCHIVE")
    $lines.Add("")
    $lines.Add("The following files exist in the project but were NOT packed. If you need one of")
    $lines.Add("them, say so - do not guess.")
    $lines.Add("")
    if ($ExcludedEntries -and $ExcludedEntries.Count -gt 0) {
        foreach ($entry in $ExcludedEntries) {
            $lines.Add($entry)
        }
    } else {
        $lines.Add("$bullet (none - every eligible project file was packed)")
    }
    $lines.Add("")
    return ($lines -join "`n")
}

$selectedFiles = [System.Collections.Generic.List[string]]::new()
$excludedEntries = [System.Collections.Generic.List[string]]::new()
$bulletChar = [string][char]45

function Record-ExcludedFile([string]$relPath, [string]$reason) {
    $relPosix = ($relPath -replace '\\', '/')
    Write-Host ("  [-] Excluded: {0,-36} ({1})" -f $relPosix, $reason) -ForegroundColor DarkYellow
    $excludedEntries.Add(("{0} {1,-32} ({2})" -f $bulletChar, $relPosix, $reason))
}

$customIgnores = @()
$gitignorePath = Join-Path $absProjectPath ".gitignore"
if (Test-Path -LiteralPath $gitignorePath) {
    $lines = Get-Content -LiteralPath $gitignorePath -Encoding UTF8
    foreach ($line in $lines) {
        $trimmed = $line.Trim()
        if ($trimmed -and -not $trimmed.StartsWith("#") -and -not $trimmed.StartsWith("!")) {
            $customIgnores += $trimmed.TrimStart('/')
        }
    }
}

if ($TargetFiles.Count -gt 0) {
    Write-Host "[+] Packaging targeted files explicitly requested..." -ForegroundColor Yellow
    foreach ($pattern in $TargetFiles) {
        $matching = Get-ChildItem -Path $absProjectPath -Filter $pattern -Recurse -File -Force -ErrorAction SilentlyContinue
        foreach ($f in $matching) {
            $rel = $f.FullName.Substring($absProjectPath.Length).TrimStart('\', '/')
            if (Test-IsDependencyOrBuildDir $rel) { continue }
            if (Test-IsSecret $f.Name) { Record-ExcludedFile $rel "looks like key material"; continue }
            if (Test-IsGeneratedArtefact $rel) { Record-ExcludedFile $rel "Argon workflow artefact"; continue }
            $ext = $f.Extension.ToLowerInvariant()
            if ($ignoreExtensions -contains $ext) { Record-ExcludedFile $rel "binary/archive extension: $ext"; continue }
            if ($f.Length -gt $maxBytes) {
                $mb = [math]::Max(1, [int][math]::Round($f.Length / 1MB))
                Record-ExcludedFile $rel "oversized: $mb MB"
                continue
            }
            if (-not $selectedFiles.Contains($rel)) {
                $selectedFiles.Add($rel)
            }
        }
    }
} else {
    Write-Host "[+] Scanning project tree (preserving all source files and reporting exclusions)..." -ForegroundColor Green
    $allFiles = Get-ChildItem -LiteralPath $absProjectPath -Recurse -File -Force -ErrorAction SilentlyContinue
    foreach ($f in $allFiles) {
        $rel = $f.FullName.Substring($absProjectPath.Length).TrimStart('\', '/')
        if (Test-IsDependencyOrBuildDir $rel) { continue }
        $segs = $rel -split '[\\/]'
        $inInternalDir = $false
        foreach ($s in $segs) {
            if (@('.argon_backup', 'downloads', '__pycache__') -contains $s.ToLowerInvariant()) {
                $inInternalDir = $true
                break
            }
        }
        if ($inInternalDir) { continue }
        if (Test-IsSecret $f.Name) { Record-ExcludedFile $rel "looks like key material"; continue }
        if (Test-IsGeneratedArtefact $rel) { Record-ExcludedFile $rel "Argon workflow artefact"; continue }
        $ext = $f.Extension.ToLowerInvariant()
        if ($ignoreExtensions -contains $ext) { Record-ExcludedFile $rel "binary/archive extension: $ext"; continue }
        $isSource = Test-IsSourceFile $f
        if ((Test-IsInAmbiguousDir $rel) -and (-not $isSource)) {
            Record-ExcludedFile $rel "non-source file in ambiguous dir ($ext)"
            continue
        }
        if (-not $isSource) {
            $skipCustom = $false
            foreach ($ign in $customIgnores) {
                if ($rel -like "*$ign*" -or $f.Name -like $ign) {
                    $skipCustom = $true
                    break
                }
            }
            if ($skipCustom) {
                Record-ExcludedFile $rel "excluded by .gitignore"
                continue
            }
        }
        if ($f.Length -gt $maxBytes) {
            $mb = [math]::Max(1, [int][math]::Round($f.Length / 1MB))
            Record-ExcludedFile $rel "oversized: $mb MB"
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
**Absolute Target Directory:** $absProjectPath
**Project Root:** $([System.IO.Path]::GetFileName($absProjectPath))
**Included Files Count:** $($selectedFiles.Count)

## User Goal / Problem Statement:
$TaskDescription

---

## CRITICAL INSTRUCTIONS FOR GEMINI 4 ARGON:

### 0. TARGET DIRECTORY (USE THESE EXACT PATHS)
The codebase you received in the archive is a copy of the real project, which lives at this absolute
directory on disk:

````
$absProjectPath
````

Your ``apply_changes.ps1`` MUST pin that directory and resolve every path against it, so the script
applies every change in one run no matter where it is executed from:

``````powershell
`$TargetRoot = '$absProjectPath'
``````

- Every file the script reads or writes is resolved with ``Join-Path `$TargetRoot '<relative path>'``.
- Never depend on the current working directory, and never guess or search for another directory.

### 1. DELIVER THE RESULT AS A FILE, NOT AS A CHAT CODE BLOCK
- Write the complete patch to a file named ``apply_changes.ps1`` and **attach it to your reply** so it
  can be downloaded with one click.
- Do NOT paste the body of the script into the chat. Keep the reply short: what changed, which files
  changed and why, the reference to the attached file, and how to verify.
- An inline code block is a fallback used only when file delivery is unavailable.

### 2. SURGICAL EDITS ONLY (ZERO UI REGRESSION):
   - Do NOT rewrite or restructure entire files unless you are creating a completely new file.
   - Do NOT modify existing UI layouts, component hierarchies, CSS/Tailwind classes, or visual styling unless explicitly requested in the task goal above.
   - For edits in existing files, use safe substring replacements (`$content.Replace(...)`) with an error throw if not found.
   - Use single-quoted Here-Strings (`@' ... '@`) so JavaScript/TypeScript `$``{var}` template literals are not corrupted.
   - Write files with UTF-8 without BOM: `[System.Text.UTF8Encoding]::new($false)`.

### 3. BACK UP BEFORE WRITING, AND APPLY EVERYTHING IN ONE RUN
   - Back up every file you overwrite into ``.argon_backup/<yyyyMMdd_HHmmss>/<relative path>``.
   - Print one ``[Patched] <relative path>`` line per changed file and a final
     ``[SUCCESS] N file(s) patched.`` summary line.
   - Abort with ``throw`` before touching anything if a target block is not found, so a partial apply
     can never happen.

### 4. VERIFY YOUR WORK (REQUIRED)
   - After writing each file, re-read it from disk and assert that the intended new content is present.
   - Print ``[Verified] <relative path>`` for every file that matches and ``[FAILED VERIFY] <relative path>``
     (exiting non-zero) if any file does not match.

### 5. RECORD WHAT YOU FOUND (REQUIRED)
   - Append a trailing comment block at the very end of ``apply_changes.ps1`` after the last write.
     The NOTES block of every delivered patch MUST start with a ``## ROOT CAUSE`` section naming the
     root cause and its evidence (even when the change is trivial):
     ``# --- NOTES: inconsistencies and suggestions found while reading the project ---``
     ``# ## ROOT CAUSE``
     ``# NOTE: <file>:<line> - <exact root cause, measured evidence, and why this change fixes it>``
     ``# NOTE: <file> - <what is inconsistent, why it matters, and what you would do instead>``
     ``# --- END NOTES ---``
"@
    $apiIndexPath = Join-Path $stagingDir "API_INDEX.md"
    $apiIndexContent = Build-ArgonApiIndex -ProjectRoot $absProjectPath -SelectedFiles $selectedFiles
    [System.IO.File]::WriteAllText($apiIndexPath, $apiIndexContent, [System.Text.UTF8Encoding]::new($false))
    $briefContent = Complete-ArgonTaskBrief -BaseBrief $briefContent -ProjectRoot $absProjectPath -SelectedFiles $selectedFiles -ExcludedEntries $excludedEntries
    [System.IO.File]::WriteAllText($briefPath, $briefContent, [System.Text.UTF8Encoding]::new($false))
    $localBriefPath = Join-Path $absProjectPath "TASK_BRIEF.md"
    if (Test-Path -LiteralPath $localBriefPath -PathType Leaf) {
        [System.IO.File]::WriteAllText($localBriefPath, $briefContent, [System.Text.UTF8Encoding]::new($false))
    }

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
