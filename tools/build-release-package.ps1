[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$CcConnectSourceRoot,
    [string]$NotifierVersion = '1.2.0',
    [string]$CcConnectBaseVersion = '1.4.1',
    [ValidateRange(1, 9999)]
    [int]$CcConnectPatchVersion = 15,
    [string]$OutputRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'dist'),
    [string]$GoPath = '',
    [string]$GoToolchain = 'go1.25.0',
    [string]$PnpmPath = '',
    [string]$PnpmVersion = '10.32.1'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$goModule = Join-Path $CcConnectSourceRoot 'go.mod'
if (-not (Test-Path -LiteralPath $goModule -PathType Leaf)) {
    throw "cc-connect go.mod not found: $goModule"
}
if (-not $GoPath) {
    $go = Get-Command go.exe, go -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $go) { throw 'Go 1.25+ was not found.' }
    $GoPath = $go.Source
}
$webRoot = Join-Path $CcConnectSourceRoot 'web'
$webLock = Join-Path $webRoot 'pnpm-lock.yaml'
if (-not (Test-Path -LiteralPath $webLock -PathType Leaf)) {
    throw "cc-connect web lockfile not found: $webLock"
}
$pnpmPrefix = @()
if (-not $PnpmPath) {
    $pnpm = Get-Command pnpm.cmd, pnpm -ErrorAction SilentlyContinue | Select-Object -First 1
    $installedPnpmVersion = if ($null -ne $pnpm) { (& $pnpm.Source --version).Trim() } else { '' }
    if ($installedPnpmVersion -match '^10\.') {
        $PnpmPath = $pnpm.Source
    } else {
        $corepack = Get-Command corepack.cmd, corepack -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $corepack) {
            throw "pnpm 10 is required; found '$installedPnpmVersion' and corepack was not found."
        }
        $PnpmPath = $corepack.Source
        $pnpmPrefix = @("pnpm@$PnpmVersion")
    }
}

$packageName = "cc-connect-codex-wechat-router-v$NotifierVersion-windows-x64"
$packageRoot = Join-Path $OutputRoot $packageName
$zipPath = Join-Path $OutputRoot "$packageName.zip"
$resolvedRepository = [IO.Path]::GetFullPath($repositoryRoot).TrimEnd('\')
$resolvedOutput = [IO.Path]::GetFullPath($OutputRoot).TrimEnd('\')
if (-not $resolvedOutput.StartsWith($resolvedRepository + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'OutputRoot must be inside the repository.'
}
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
foreach ($target in @($packageRoot, $zipPath, "$zipPath.sha256")) {
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
}
New-Item -ItemType Directory -Path (Join-Path $packageRoot 'bin') -Force | Out-Null

$ccConnectCommit = (& git -c "safe.directory=$CcConnectSourceRoot" -C $CcConnectSourceRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $ccConnectCommit) {
    throw 'Could not resolve the cc-connect source commit.'
}
$ccConnectVersion = "v$CcConnectBaseVersion+qr$CcConnectPatchVersion"
$buildTime = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
$ccConnectBinary = Join-Path $packageRoot 'bin\cc-connect.exe'
$ldFlags = "-X main.version=$ccConnectVersion -X main.commit=$($ccConnectCommit.Substring(0, 12)) -X main.buildTime=$buildTime"
$previousCi = $env:CI
$previousToolchain = $env:GOTOOLCHAIN
$env:CI = 'true'
$env:GOTOOLCHAIN = $GoToolchain
try {
    Push-Location $webRoot
    try {
        & $PnpmPath @pnpmPrefix install --frozen-lockfile
        if ($LASTEXITCODE -ne 0) { throw "pnpm install failed with exit code $LASTEXITCODE" }
        & $PnpmPath @pnpmPrefix build
        if ($LASTEXITCODE -ne 0) { throw "pnpm build failed with exit code $LASTEXITCODE" }
    }
    finally {
        Pop-Location
    }
    Push-Location $CcConnectSourceRoot
    try {
        & $GoPath build -buildvcs=false -trimpath -ldflags $ldFlags -o $ccConnectBinary ./cmd/cc-connect
        if ($LASTEXITCODE -ne 0) { throw "go build failed with exit code $LASTEXITCODE" }
    }
    finally {
        Pop-Location
    }
}
finally {
    $env:CI = $previousCi
    $env:GOTOOLCHAIN = $previousToolchain
}

$binaryHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ccConnectBinary).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$ccConnectBinary.sha256" `
    -Value "$binaryHash  cc-connect.exe" -Encoding ascii

$runtimeFiles = @(
    'setup.ps1',
    'install.ps1',
    'notifier.py',
    'desktop_cdp_transport.py',
    'websocket_transport.py',
    'desktop-shared-channel.ps1',
    'config.example.json',
    'cc-connect.example.toml',
    'README.md',
    'README.en.md',
    'CHANGELOG.md',
    'LICENSE',
    'NOTICE.md',
    'THIRD_PARTY_NOTICES.md'
)
foreach ($relativePath in $runtimeFiles) {
    $source = Join-Path $repositoryRoot $relativePath
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Release input not found: $source"
    }
    Copy-Item -LiteralPath $source -Destination (Join-Path $packageRoot $relativePath) -Force
}

$manifest = [ordered]@{
    notifier_version = $NotifierVersion
    cc_connect_version = $ccConnectVersion
    cc_connect_repository = 'https://github.com/JiaYang-BUAA/cc-connect'
    cc_connect_source_commit = $ccConnectCommit
    cc_connect_binary_sha256 = $binaryHash
    built_at = $buildTime
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $packageRoot 'release-manifest.json') -Encoding utf8

Compress-Archive -Path (Join-Path $packageRoot '*') -DestinationPath $zipPath -CompressionLevel Optimal
$zipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$zipPath.sha256" `
    -Value "$zipHash  $([IO.Path]::GetFileName($zipPath))" -Encoding ascii

[pscustomobject]@{
    Package = $packageRoot
    Archive = $zipPath
    ArchiveSha256 = $zipHash
    CcConnectVersion = $ccConnectVersion
    CcConnectCommit = $ccConnectCommit
}
