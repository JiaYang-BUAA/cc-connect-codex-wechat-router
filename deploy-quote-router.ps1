[CmdletBinding()]
param(
    [ValidateRange(1, 9999)]
    [int]$PatchVersion = 15,
    [string]$BaseVersion = '1.4.1',
    [string]$SourceRoot = (Join-Path (Split-Path -Parent $PSCommandPath) 'artifacts'),
    [string]$Target = '',
    [string]$BackupRoot = (Join-Path (Split-Path -Parent $PSCommandPath) 'backups'),
    [string]$NotifierConfig = (Join-Path (Split-Path -Parent $PSCommandPath) 'config.json'),
    [string]$DaemonLog = (Join-Path $env:USERPROFILE '.cc-connect\logs\daemon.log'),
    [string]$ConnectorTask = 'cc-connect',
    [string]$NotifierTask = 'Codex Pinned WeChat Notifier'
)

$ErrorActionPreference = 'Stop'
$ccCommand = if ($Target) { $null } else { Get-Command cc-connect.exe -ErrorAction SilentlyContinue }
if (-not $Target) {
    if ($null -eq $ccCommand) { throw 'cc-connect.exe was not found. Pass -Target with the installed executable path.' }
    $Target = $ccCommand.Source
}
$Source = Join-Path $SourceRoot "cc-connect-v$BaseVersion-quote-router.$PatchVersion.exe"
$HashPath = "$Source.sha256"
$ExpectedVersion = "v$BaseVersion+qr$PatchVersion"
$BackupDir = Join-Path $BackupRoot ((Get-Date).ToString('yyyyMMdd-HHmmss') + "-quote-router.$PatchVersion")
$Backup = Join-Path $BackupDir 'cc-connect-before.exe'
$Installed = $false
$DaemonLogOffset = if (Test-Path -LiteralPath $DaemonLog) {
    (Get-Item -LiteralPath $DaemonLog).Length
} else {
    0
}

foreach ($Path in @($Source, $HashPath, $Target, $NotifierConfig)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file not found: $Path"
    }
}

$ExpectedHash = ((Get-Content -Raw -LiteralPath $HashPath).Trim() -split '\s+')[0].ToLowerInvariant()
$ActualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Source).Hash.ToLowerInvariant()
if ($ExpectedHash -ne $ActualHash) {
    throw "SHA-256 mismatch for $Source"
}

$Config = Get-Content -Raw -LiteralPath $NotifierConfig | ConvertFrom-Json
$HealthUri = "http://$($Config.router_host):$($Config.router_port)/healthz"
$Headers = @{ 'X-Codex-Quote-Token' = [string]$Config.router_token }
$ConnectorWasRunning = (Get-ScheduledTask -TaskName $ConnectorTask -ErrorAction SilentlyContinue).State -eq 'Running'
$NotifierWasRunning = (Get-ScheduledTask -TaskName $NotifierTask -ErrorAction SilentlyContinue).State -eq 'Running'

New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
Copy-Item -LiteralPath $Target -Destination $Backup -Force

try {
    Stop-ScheduledTask -TaskName $NotifierTask -ErrorAction SilentlyContinue
    Stop-ScheduledTask -TaskName $ConnectorTask -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Get-Process -Name 'cc-connect' -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue

    # Stopping cc-connect also terminates its child Codex app-server. That can
    # emit an expected ERROR entry, so only validate logs written after the old
    # daemon has fully stopped.
    $DaemonLogOffset = if (Test-Path -LiteralPath $DaemonLog) {
        (Get-Item -LiteralPath $DaemonLog).Length
    } else {
        0
    }

    Copy-Item -LiteralPath $Source -Destination $Target -Force
    $Installed = $true
    Start-ScheduledTask -TaskName $ConnectorTask
    Start-Sleep -Seconds 8
    Start-ScheduledTask -TaskName $NotifierTask

    $VersionOutput = (& $Target --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $VersionOutput -notmatch [regex]::Escape($ExpectedVersion)) {
        throw "Installed version verification failed: $VersionOutput"
    }

    $Deadline = (Get-Date).AddSeconds(20)
    do {
        try {
            $Health = Invoke-RestMethod -Method Get -Uri $HealthUri -Headers $Headers -TimeoutSec 3
        }
        catch {
            $Health = $null
            Start-Sleep -Milliseconds 500
        }
    } while (($null -eq $Health -or -not $Health.ok) -and (Get-Date) -lt $Deadline)
    if ($null -eq $Health -or -not $Health.ok) {
        throw "Notifier health check failed: $HealthUri"
    }

    if (Test-Path -LiteralPath $DaemonLog) {
        $Stream = [IO.File]::Open($DaemonLog, 'Open', 'Read', 'ReadWrite')
        try {
            if ($Stream.Length -ge $DaemonLogOffset) {
                [void]$Stream.Seek($DaemonLogOffset, 'Begin')
            }
            $Reader = [IO.StreamReader]::new($Stream)
            try {
                $NewLogText = $Reader.ReadToEnd()
            }
            finally {
                $Reader.Dispose()
            }
        }
        finally {
            $Stream.Dispose()
        }
        if ($NewLogText -match '(?im)level=(ERROR|FATAL)|\bpanic\b') {
            throw 'cc-connect daemon reported a new fatal/error log entry'
        }
    }

    [pscustomobject]@{
        Deployed = $true
        Version = $ExpectedVersion
        Backup = $Backup
        NotifierHealth = $Health.ok
        SubmitTransport = $Health.submit_transport
    }
}
catch {
    $Failure = $_
    if ($Installed -and (Test-Path -LiteralPath $Backup -PathType Leaf)) {
        Stop-ScheduledTask -TaskName $NotifierTask -ErrorAction SilentlyContinue
        Stop-ScheduledTask -TaskName $ConnectorTask -ErrorAction SilentlyContinue
        Get-Process -Name 'cc-connect' -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
        Copy-Item -LiteralPath $Backup -Destination $Target -Force
    }
    if ($ConnectorWasRunning) {
        Start-ScheduledTask -TaskName $ConnectorTask -ErrorAction SilentlyContinue
    }
    if ($NotifierWasRunning) {
        Start-ScheduledTask -TaskName $NotifierTask -ErrorAction SilentlyContinue
    }
    throw "Deployment failed and previous executable was restored: $($Failure.Exception.Message)"
}
