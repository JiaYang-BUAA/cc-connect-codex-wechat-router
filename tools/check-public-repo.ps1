$ErrorActionPreference = 'Stop'
$repository = (Get-Location).Path
$tracked = @(git -c "safe.directory=$repository" ls-files)
if (-not $tracked) { throw 'No tracked files found; initialize Git before running the public repository check.' }
$forbidden = @(
    'C:\Users\yang', 'E:\codex', 'router_token": "d',
    'o9cq8084'
)
$matches = foreach ($path in $tracked) {
    if ($path -in @('tools/check-public-repo.ps1', 'tools\check-public-repo.ps1')) { continue }
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $text = Get-Content -Raw -LiteralPath $path
        foreach ($needle in $forbidden) {
            if ($text.Contains($needle)) { "${path}: $needle" }
        }
    }
}
if ($matches) { $matches | ForEach-Object { Write-Error $_ }; exit 1 }
Write-Output "Public repository scan passed ($($tracked.Count) tracked files)."
