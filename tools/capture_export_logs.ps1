param(
    [string]$Python = "python",
    [string]$NewScript = "",
    [string]$LegacyScript = "C:\Users\micha\hello\Chats\ChatMain\export_chat_archive.py",
    [string]$OutputDir = "",
    [switch]$UseConfiguredArchive,
    [switch]$PrintOnly,
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = "Stop"

$ToolDir = Split-Path -Parent $PSCommandPath
$RepoRoot = Split-Path -Parent $ToolDir
if (-not $NewScript) {
    $NewScript = Join-Path $RepoRoot "export_chat_archive.py"
}
if (-not $OutputDir) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $suffix = [guid]::NewGuid().ToString("N").Substring(0, 6)
    $OutputDir = Join-Path $RepoRoot "export_log_runs\$stamp-$suffix"
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$NewLog = Join-Path $OutputDir "chat2markdown.raw.log"
$LegacyLog = Join-Path $OutputDir "chatmain.raw.log"
$NewNormLog = Join-Path $OutputDir "chat2markdown.normalized.log"
$LegacyNormLog = Join-Path $OutputDir "chatmain.normalized.log"
$DiffPath = Join-Path $OutputDir "normalized.diff.txt"
$CommandPath = Join-Path $OutputDir "commands.txt"

$NewArchiveRoot = Join-Path $OutputDir "chat2markdown_archive"
$LegacyArchiveRoot = Join-Path $OutputDir "chatmain_archive"

function Format-CommandLine {
    param([string]$Program, [string[]]$Arguments)
    $quoted = foreach ($arg in $Arguments) {
        if ($arg -match '\s') { '"' + $arg.Replace('"', '\"') + '"' } else { $arg }
    }
    return "$Program $($quoted -join ' ')"
}

function Invoke-LoggedExport {
    param(
        [string]$Label,
        [string]$ScriptPath,
        [string]$LogPath,
        [string]$ArchiveRoot
    )

    $workDir = Split-Path -Parent $ScriptPath
    $arguments = @("-u", $ScriptPath)
    if (-not $UseConfiguredArchive) {
        $arguments += @("--archive-root", $ArchiveRoot)
    }
    $arguments += $ExtraArgs

    $commandLine = Format-CommandLine -Program $Python -Arguments $arguments
    Add-Content -Path $CommandPath -Value "[$Label]"
    Add-Content -Path $CommandPath -Value "workdir=$workDir"
    Add-Content -Path $CommandPath -Value $commandLine
    Add-Content -Path $CommandPath -Value ""

    Write-Host ""
    Write-Host "[$Label]"
    Write-Host "workdir=$workDir"
    Write-Host $commandLine

    if ($PrintOnly) {
        return 0
    }

    Push-Location $workDir
    try {
        & $Python @arguments 2>&1 | Tee-Object -FilePath $LogPath
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    Set-Content -Path (Join-Path $OutputDir "$Label.exit.txt") -Value $exitCode
    return $exitCode
}

function Convert-ToNormalizedLog {
    param([string]$SourcePath, [string]$DestinationPath)

    if (-not (Test-Path $SourcePath)) {
        return
    }

    $homePath = [regex]::Escape([string][Environment]::GetFolderPath("UserProfile"))
    $repoPath = [regex]::Escape($RepoRoot)
    $newArchive = [regex]::Escape($NewArchiveRoot)
    $legacyArchive = [regex]::Escape($LegacyArchiveRoot)
    $newScriptPath = [regex]::Escape($NewScript)
    $legacyScriptPath = [regex]::Escape($LegacyScript)

    $text = Get-Content -Raw -Path $SourcePath
    $text = $text -replace $newArchive, "<ARCHIVE_ROOT>"
    $text = $text -replace $legacyArchive, "<ARCHIVE_ROOT>"
    $text = $text -replace $newScriptPath, "<EXPORT_SCRIPT>"
    $text = $text -replace $legacyScriptPath, "<EXPORT_SCRIPT>"
    $text = $text -replace $repoPath, "<REPO_ROOT>"
    $text = $text -replace $homePath, "%USER%"
    $text = $text -replace "\\", "/"

    Set-Content -Path $DestinationPath -Value $text
}

if ($UseConfiguredArchive) {
    Write-Host "Mode: exact configured archive paths. This can write/prune the live archive twice."
}
else {
    Write-Host "Mode: isolated comparison archives under $OutputDir"
}

$newExit = Invoke-LoggedExport -Label "chat2markdown" -ScriptPath $NewScript -LogPath $NewLog -ArchiveRoot $NewArchiveRoot
$legacyExit = Invoke-LoggedExport -Label "chatmain" -ScriptPath $LegacyScript -LogPath $LegacyLog -ArchiveRoot $LegacyArchiveRoot

if (-not $PrintOnly) {
    Convert-ToNormalizedLog -SourcePath $NewLog -DestinationPath $NewNormLog
    Convert-ToNormalizedLog -SourcePath $LegacyLog -DestinationPath $LegacyNormLog

    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        & git --no-pager diff --no-index -- $NewNormLog $LegacyNormLog > $DiffPath
        $diffExit = $LASTEXITCODE
        if ($diffExit -eq 0) {
            "Logs match after normalization." | Set-Content -Path $DiffPath
        }
    }
    else {
        Compare-Object (Get-Content $NewNormLog) (Get-Content $LegacyNormLog) | Out-File -FilePath $DiffPath
    }
}

Write-Host ""
Write-Host "Output folder: $OutputDir"
Write-Host "Commands: $CommandPath"
Write-Host "New raw log: $NewLog"
Write-Host "Legacy raw log: $LegacyLog"
Write-Host "Normalized diff: $DiffPath"
Write-Host "Exit codes: chat2markdown=$newExit chatmain=$legacyExit"
