[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$ArchivePath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$archive = (Resolve-Path -LiteralPath $ArchivePath).Path
$archiveHashPath = "$archive.sha256"
if (-not (Test-Path -LiteralPath $archiveHashPath -PathType Leaf)) {
    throw "Package checksum not found: $archiveHashPath"
}
$expectedArchiveHash = ((Get-Content -Raw -LiteralPath $archiveHashPath).Trim() -split '\s+')[0]
$actualArchiveHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash
if ($expectedArchiveHash -ne $actualArchiveHash) {
    throw 'Release archive failed SHA-256 verification.'
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('cc-connect-router-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
try {
    $package = Join-Path $testRoot 'package'
    Expand-Archive -LiteralPath $archive -DestinationPath $package
    $setup = Join-Path $package 'setup.ps1'
    $binary = Join-Path $package 'bin\cc-connect.exe'
    foreach ($path in @($setup, $binary, "$binary.sha256", (Join-Path $package 'release-manifest.json'))) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Package input not found after extraction: $path"
        }
    }
    $ccConfig = Join-Path $testRoot 'config.toml'
    @'
language = "zh"

[[projects]]
name = "test-project"

[projects.agent]
type = "codex"

[projects.agent.options]
work_dir = "C:/workspace"
backend = "app-server"
app_server_url = "ws://127.0.0.1:18766"

[[projects.platforms]]
type = "weixin"

[projects.platforms.options]
token = "test-token"
allow_from = "test-user"
'@ | Set-Content -LiteralPath $ccConfig -Encoding utf8
    $codexDb = Join-Path $testRoot 'state_5.sqlite'
    New-Item -ItemType File -Path $codexDb -Force | Out-Null
    $python = (Get-Command python.exe, python | Select-Object -First 1).Source
    $result = & $setup `
        -InstallRoot (Join-Path $testRoot 'install') `
        -CcConnectConfig $ccConfig `
        -CcProject 'test-project' `
        -CodexDb $codexDb `
        -CodexCli $env:COMSPEC `
        -PythonPath $python `
        -SkipServiceInstall
    $notifierConfig = Get-Content -Raw -LiteralPath $result.NotifierConfig | ConvertFrom-Json
    $updatedToml = Get-Content -Raw -LiteralPath $ccConfig
    if (-not $result.Installed -or $result.ServicesInstalled) {
        throw 'Guided installer returned an invalid result.'
    }
    if (-not (Test-Path -LiteralPath $notifierConfig.cc_connect -PathType Leaf)) {
        throw 'Installed cc-connect path is missing.'
    }
    if ([string]$notifierConfig.cc_project -ne 'test-project') {
        throw 'The selected project was not preserved.'
    }
    if ([string]$notifierConfig.router_token -notmatch '^[-_A-Za-z0-9]{40,}$') {
        throw 'The generated router token is invalid.'
    }
    if ($updatedToml -notmatch 'codex_quote_router_url\s*=\s*"http://127\.0\.0\.1:18765/route"') {
        throw 'The router URL was not written to cc-connect config.'
    }
    if ($updatedToml -notmatch [regex]::Escape([string]$notifierConfig.router_token)) {
        throw 'The shared router token was not written to cc-connect config.'
    }
    if (-not (Test-Path -LiteralPath $result.CcConnectConfigBackup -PathType Leaf)) {
        throw 'The cc-connect config backup is missing.'
    }
    Write-Output 'Combined release package test passed.'
}
finally {
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    $resolvedTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if ($resolvedTestRoot.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
