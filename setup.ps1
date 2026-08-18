[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'cc-connect-codex-wechat-router'),
    [string]$BundledCcConnectPath = (Join-Path $PSScriptRoot 'bin\cc-connect.exe'),
    [string]$CcConnectConfig = (Join-Path $env:USERPROFILE '.cc-connect\config.toml'),
    [string]$CcProject = '',
    [string]$CodexDb = (Join-Path $env:USERPROFILE '.codex\state_5.sqlite'),
    [string]$CodexCli = '',
    [string]$PythonPath = '',
    [switch]$SkipServiceInstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-QuotedTomlValue {
    param([string]$Line, [string]$Key)
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*=\s*["''](?<value>[^"'']+)["'']'
    $match = [regex]::Match($Line, $pattern)
    if ($match.Success) { return $match.Groups['value'].Value }
    return ''
}

function Get-WeixinPlatforms {
    param([string[]]$Lines)
    $projectStarts = @(
        for ($index = 0; $index -lt $Lines.Count; $index++) {
            if ($Lines[$index].Trim() -eq '[[projects]]') { $index }
        }
    )
    $results = @()
    for ($projectNumber = 0; $projectNumber -lt $projectStarts.Count; $projectNumber++) {
        $projectStart = $projectStarts[$projectNumber]
        $projectEnd = if ($projectNumber + 1 -lt $projectStarts.Count) {
            $projectStarts[$projectNumber + 1] - 1
        } else {
            $Lines.Count - 1
        }
        $projectName = ''
        for ($index = $projectStart + 1; $index -le $projectEnd; $index++) {
            $projectName = Get-QuotedTomlValue -Line $Lines[$index] -Key 'name'
            if ($projectName) { break }
        }
        $platformStarts = @(
            for ($index = $projectStart + 1; $index -le $projectEnd; $index++) {
                if ($Lines[$index].Trim() -eq '[[projects.platforms]]') { $index }
            }
        )
        for ($platformNumber = 0; $platformNumber -lt $platformStarts.Count; $platformNumber++) {
            $platformStart = $platformStarts[$platformNumber]
            $platformEnd = if ($platformNumber + 1 -lt $platformStarts.Count) {
                $platformStarts[$platformNumber + 1] - 1
            } else {
                $projectEnd
            }
            $platformType = ''
            $optionsIndex = -1
            for ($index = $platformStart + 1; $index -le $platformEnd; $index++) {
                if (-not $platformType) {
                    $platformType = Get-QuotedTomlValue -Line $Lines[$index] -Key 'type'
                }
                if ($Lines[$index].Trim() -eq '[projects.platforms.options]') {
                    $optionsIndex = $index
                    break
                }
            }
            if ($platformType -eq 'weixin' -and $optionsIndex -ge 0) {
                $results += [pscustomobject]@{
                    Project = $projectName
                    OptionsIndex = $optionsIndex
                    PlatformEnd = $platformEnd
                }
            }
        }
    }
    return $results
}

function Set-TomlOption {
    param(
        [System.Collections.Generic.List[string]]$Lines,
        [int]$OptionsIndex,
        [string]$Key,
        [string]$Value
    )
    $escaped = $Value.Replace('\', '\\').Replace('"', '\"')
    $replacement = "$Key = `"$escaped`""
    $sectionEnd = $OptionsIndex + 1
    while ($sectionEnd -lt $Lines.Count -and $Lines[$sectionEnd] -notmatch '^\s*\[') {
        if ($Lines[$sectionEnd] -match ('^\s*' + [regex]::Escape($Key) + '\s*=')) {
            $Lines[$sectionEnd] = $replacement
            return
        }
        $sectionEnd++
    }
    $Lines.Insert($sectionEnd, $replacement)
}

function New-RouterToken {
    $bytes = [byte[]]::new(32)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Resolve-CommandPath {
    param([string]$ExplicitPath, [string[]]$Names, [string[]]$Fallbacks)
    if ($ExplicitPath) {
        if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) {
            throw "Executable not found: $ExplicitPath"
        }
        return (Resolve-Path -LiteralPath $ExplicitPath).Path
    }
    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $command) { return $command.Source }
    }
    foreach ($fallback in $Fallbacks) {
        if ($fallback -and (Test-Path -LiteralPath $fallback -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $fallback).Path
        }
    }
    return ''
}

if ($env:OS -ne 'Windows_NT') {
    throw 'This installer supports Windows only.'
}
if (-not (Test-Path -LiteralPath $BundledCcConnectPath -PathType Leaf)) {
    throw "Bundled cc-connect.exe was not found: $BundledCcConnectPath. Download the ZIP from this repository's Releases page."
}
$bundledHashPath = "$BundledCcConnectPath.sha256"
if (-not (Test-Path -LiteralPath $bundledHashPath -PathType Leaf)) {
    throw "Bundled checksum was not found: $bundledHashPath"
}
$expectedHash = ((Get-Content -Raw -LiteralPath $bundledHashPath).Trim() -split '\s+')[0].ToLowerInvariant()
$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $BundledCcConnectPath).Hash.ToLowerInvariant()
if ($expectedHash -ne $actualHash) {
    throw 'Bundled cc-connect.exe failed SHA-256 verification.'
}

$CodexCli = Resolve-CommandPath -ExplicitPath $CodexCli -Names @('codex.exe', 'codex') -Fallbacks @(
    (Join-Path $env:LOCALAPPDATA 'Programs\OpenAI Codex CLI\codex.exe')
)
if (-not $CodexCli) {
    throw 'Codex CLI was not found. Install Codex Desktop/CLI or pass -CodexCli.'
}
$PythonPath = Resolve-CommandPath -ExplicitPath $PythonPath -Names @('pythonw.exe', 'python.exe', 'python') -Fallbacks @()
if (-not $PythonPath) {
    throw 'Python 3.11+ was not found. Install Python or pass -PythonPath.'
}
if (-not (Test-Path -LiteralPath $CodexDb -PathType Leaf)) {
    throw "Codex Desktop database was not found: $CodexDb"
}

$sourceRoot = $PSScriptRoot
$installFiles = @(
    'notifier.py',
    'desktop_cdp_transport.py',
    'websocket_transport.py',
    'desktop-shared-channel.ps1',
    'install.ps1',
    'config.example.json',
    'cc-connect.example.toml',
    'README.md',
    'README.en.md',
    'CHANGELOG.md',
    'LICENSE',
    'NOTICE.md',
    'THIRD_PARTY_NOTICES.md',
    'release-manifest.json'
)
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
foreach ($relativePath in $installFiles) {
    $source = Join-Path $sourceRoot $relativePath
    if (Test-Path -LiteralPath $source -PathType Leaf) {
        $destination = Join-Path $InstallRoot $relativePath
        if ([IO.Path]::GetFullPath($source) -ne [IO.Path]::GetFullPath($destination)) {
            Copy-Item -LiteralPath $source -Destination $destination -Force
        }
    }
}

$targetBinDir = Join-Path $InstallRoot 'bin'
New-Item -ItemType Directory -Path $targetBinDir -Force | Out-Null
$targetCcConnect = Join-Path $targetBinDir 'cc-connect.exe'
if (-not $SkipServiceInstall) {
    Stop-ScheduledTask -TaskName 'Codex Pinned WeChat Notifier' -ErrorAction SilentlyContinue
    Stop-ScheduledTask -TaskName 'cc-connect' -ErrorAction SilentlyContinue
}
if (Test-Path -LiteralPath $targetCcConnect -PathType Leaf) {
    $backupDir = Join-Path $InstallRoot ('backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    Copy-Item -LiteralPath $targetCcConnect -Destination (Join-Path $backupDir 'cc-connect.exe') -Force
}
if ([IO.Path]::GetFullPath($BundledCcConnectPath) -ne [IO.Path]::GetFullPath($targetCcConnect)) {
    Copy-Item -LiteralPath $BundledCcConnectPath -Destination $targetCcConnect -Force
}
Copy-Item -LiteralPath $bundledHashPath -Destination "$targetCcConnect.sha256" -Force

if (-not (Test-Path -LiteralPath $CcConnectConfig -PathType Leaf)) {
    $configParent = Split-Path -Parent $CcConnectConfig
    New-Item -ItemType Directory -Path $configParent -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $InstallRoot 'cc-connect.example.toml') `
        -Destination $CcConnectConfig -Force
    if (-not $CcProject) { $CcProject = 'codex' }
    Write-Host '首次安装：请使用微信扫描下面的二维码完成机器人登录。' -ForegroundColor Cyan
    & $targetCcConnect weixin setup `
        -config $CcConnectConfig `
        -project $CcProject `
        -set-allow-from-empty
    if ($LASTEXITCODE -ne 0) {
        throw "Weixin setup failed with exit code $LASTEXITCODE"
    }
}

$notifierConfigPath = Join-Path $InstallRoot 'config.json'
$existingNotifierConfig = if (Test-Path -LiteralPath $notifierConfigPath -PathType Leaf) {
    Get-Content -Raw -LiteralPath $notifierConfigPath | ConvertFrom-Json
} else {
    $null
}
if (-not $CcProject -and $null -ne $existingNotifierConfig) {
    $CcProject = [string]$existingNotifierConfig.cc_project
}

$tomlLines = @(Get-Content -LiteralPath $CcConnectConfig)
$weixinPlatforms = @(Get-WeixinPlatforms -Lines $tomlLines)
if ($CcProject) {
    $weixinPlatforms = @($weixinPlatforms | Where-Object { $_.Project -eq $CcProject })
}
if ($weixinPlatforms.Count -ne 1) {
    $names = @($weixinPlatforms | ForEach-Object { $_.Project } | Sort-Object -Unique)
    $detail = if ($names.Count) { $names -join ', ' } else { 'none' }
    throw "Could not select exactly one Weixin project. Available matches: $detail. Pass -CcProject with the project name after completing cc-connect Weixin setup."
}
$selected = $weixinPlatforms[0]
$CcProject = [string]$selected.Project
$routerToken = if ($null -ne $existingNotifierConfig -and [string]$existingNotifierConfig.router_token) {
    [string]$existingNotifierConfig.router_token
} else {
    New-RouterToken
}

$mutableToml = [System.Collections.Generic.List[string]]::new()
$tomlLines | ForEach-Object { [void]$mutableToml.Add($_) }
Set-TomlOption -Lines $mutableToml -OptionsIndex $selected.OptionsIndex `
    -Key 'codex_quote_router_url' -Value 'http://127.0.0.1:18765/route'
Set-TomlOption -Lines $mutableToml -OptionsIndex $selected.OptionsIndex `
    -Key 'codex_quote_router_token' -Value $routerToken
$tomlBackup = "$CcConnectConfig.backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
Copy-Item -LiteralPath $CcConnectConfig -Destination $tomlBackup -Force
$mutableToml | Set-Content -LiteralPath $CcConnectConfig -Encoding utf8

$templatePath = Join-Path $InstallRoot 'config.example.json'
$config = Get-Content -Raw -LiteralPath $templatePath | ConvertFrom-Json
$config.codex_db = (Resolve-Path -LiteralPath $CodexDb).Path
$config.cc_connect = $targetCcConnect
$config.cc_project = $CcProject
$config.codex_cli = $CodexCli
$config.state_file = Join-Path $InstallRoot 'data\state.json'
$config.log_file = Join-Path $InstallRoot 'logs\notifier.log'
$config.router_token = $routerToken
$config.codex_submit_transport = 'desktop-cdp'
$config | Add-Member -NotePropertyName cc_connect_config -NotePropertyValue $CcConnectConfig -Force
$config | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $notifierConfigPath -Encoding utf8

if (-not $SkipServiceInstall) {
    & $targetCcConnect daemon install --config $CcConnectConfig
    if ($LASTEXITCODE -ne 0) { throw "cc-connect daemon install failed with exit code $LASTEXITCODE" }
    & (Join-Path $InstallRoot 'install.ps1') -PythonPath $PythonPath -ConfigPath $notifierConfigPath
    if ($LASTEXITCODE -ne 0) { throw "Notifier installation failed with exit code $LASTEXITCODE" }
}

[pscustomobject]@{
    Installed = $true
    InstallRoot = $InstallRoot
    CcConnect = $targetCcConnect
    CcConnectConfig = $CcConnectConfig
    CcConnectConfigBackup = $tomlBackup
    CcProject = $CcProject
    NotifierConfig = $notifierConfigPath
    ServicesInstalled = -not $SkipServiceInstall
}
