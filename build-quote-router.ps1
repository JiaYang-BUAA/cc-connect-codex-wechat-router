[CmdletBinding()]
param(
    [ValidateRange(1, 9999)]
    [int]$PatchVersion = 15,
    [string]$BaseVersion = '1.4.1',
    [string]$SourceRoot = (Join-Path (Split-Path -Parent $PSCommandPath) '..\cc-connect'),
    [string]$OutputRoot = (Join-Path (Split-Path -Parent $PSCommandPath) 'artifacts'),
    [string]$GoPath = '',
    [string]$GoWorkRoot = (Join-Path (Split-Path -Parent $PSCommandPath) '.cache\go-work')
)

$ErrorActionPreference = 'Stop'

$GoModule = Join-Path $SourceRoot 'go.mod'
if (-not (Test-Path -LiteralPath $GoModule -PathType Leaf)) {
    throw "Go module not found: $GoModule"
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
if (-not $GoPath) {
    $GoCommand = Get-Command go -ErrorAction SilentlyContinue
    if ($null -eq $GoCommand) {
        throw 'Go toolchain not found. Install Go or pass -GoPath with the full path to go.exe.'
    }
    $GoPath = $GoCommand.Source
}
if (-not (Test-Path -LiteralPath $GoPath -PathType Leaf)) {
    throw "Go executable not found: $GoPath"
}
New-Item -ItemType Directory -Path (Join-Path $GoWorkRoot 'tmp'), (Join-Path $GoWorkRoot 'cache'), (Join-Path $GoWorkRoot 'mod') -Force | Out-Null
$env:GOTOOLCHAIN = 'local'
$env:GOPROXY = 'https://goproxy.cn,direct'
$env:GOSUMDB = 'sum.golang.google.cn'
$env:GOTMPDIR = Join-Path $GoWorkRoot 'tmp'
$env:GOCACHE = Join-Path $GoWorkRoot 'cache'
$env:GOMODCACHE = Join-Path $GoWorkRoot 'mod'
$env:GOMAXPROCS = '1'
$Version = "v$BaseVersion+qr$PatchVersion"
$Commit = 'unversioned'
if (Test-Path -LiteralPath (Join-Path $SourceRoot '.git')) {
    $ResolvedCommit = & git -c "safe.directory=$SourceRoot" -C $SourceRoot rev-parse --short=12 HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and $ResolvedCommit) {
        $Commit = $ResolvedCommit.Trim()
    }
}
$BuildTime = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
$OutputPath = Join-Path $OutputRoot "cc-connect-v$BaseVersion-quote-router.$PatchVersion.exe"
$LdFlags = "-X main.version=$Version -X main.commit=$Commit -X main.buildTime=$BuildTime"

Push-Location $SourceRoot
try {
    & $GoPath build -buildvcs=false -trimpath -ldflags $LdFlags -o $OutputPath ./cmd/cc-connect
    if ($LASTEXITCODE -ne 0) {
        throw "go build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $OutputPath).Hash.ToLowerInvariant()
$HashPath = "$OutputPath.sha256"
Set-Content -LiteralPath $HashPath -Value "$Hash  $([IO.Path]::GetFileName($OutputPath))" -Encoding ascii

[pscustomobject]@{
    Version = $Version
    Commit = $Commit
    Executable = $OutputPath
    Sha256 = $Hash
    HashFile = $HashPath
}
