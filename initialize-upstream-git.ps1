[CmdletBinding()]
param(
    [string]$SourceRoot = (Join-Path (Split-Path -Parent $PSCommandPath) '..\cc-connect'),
    [string]$Upstream = 'https://github.com/chenhg5/cc-connect.git',
    [string]$Tag = 'v1.4.1',
    [string]$Branch = 'quote-router'
)

$ErrorActionPreference = 'Stop'
$GitDir = Join-Path $SourceRoot '.git'
if (Test-Path -LiteralPath $GitDir) {
    throw "Git repository already exists: $GitDir"
}

Push-Location $SourceRoot
try {
    & git -c "safe.directory=$SourceRoot" init -b $Branch
    & git -c "safe.directory=$SourceRoot" remote add upstream $Upstream
    & git -c "safe.directory=$SourceRoot" fetch --depth=1 upstream "refs/tags/$Tag`:refs/tags/$Tag"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to fetch upstream tag $Tag"
    }
    $UpstreamCommit = (& git -c "safe.directory=$SourceRoot" rev-parse "refs/tags/$Tag`^{commit}").Trim()
    & git -c "safe.directory=$SourceRoot" update-ref "refs/heads/$Branch" $UpstreamCommit
    & git -c "safe.directory=$SourceRoot" read-tree $UpstreamCommit
    & git -c "safe.directory=$SourceRoot" status --short
    Write-Host "Upstream baseline ready at $UpstreamCommit. Review, then commit the listed custom changes."
}
catch {
    if (Test-Path -LiteralPath $GitDir) {
        Remove-Item -LiteralPath $GitDir -Recurse -Force
    }
    throw
}
finally {
    Pop-Location
}
