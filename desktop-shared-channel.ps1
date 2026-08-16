[CmdletBinding()]
param(
    [ValidateSet('Enable', 'Disable')]
    [string]$Mode = 'Enable',
    [ValidateRange(1024, 65535)]
    [int]$Port = 18766,
    [string]$TaskName = 'Codex Pinned WeChat Notifier',
    [switch]$NoDesktopRestart
)

$ErrorActionPreference = 'Stop'

$ProjectPath = Split-Path -Parent $PSCommandPath
$ConfigPath = Join-Path $ProjectPath 'config.json'
$InstallPath = Join-Path $ProjectPath 'install.ps1'
$WebSocketUrl = "ws://127.0.0.1:$Port"

function Set-ConfigProperty {
    param(
        [Parameter(Mandatory)] [psobject]$Config,
        [Parameter(Mandatory)] [string]$Name,
        [Parameter(Mandatory)] $Value
    )

    if ($null -eq $Config.PSObject.Properties[$Name]) {
        $Config | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    } else {
        $Config.$Name = $Value
    }
}

function Resolve-CodexDesktopPaths {
    $Package = Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction SilentlyContinue |
        Sort-Object Version -Descending |
        Select-Object -First 1

    if ($null -ne $Package -and $Package.InstallLocation) {
        $AppDirectory = Join-Path $Package.InstallLocation 'app'
    } else {
        $ChatGptPath = Get-Process ChatGPT -ErrorAction SilentlyContinue |
            Where-Object Path |
            Select-Object -First 1 -ExpandProperty Path
        if (-not $ChatGptPath) {
            throw 'Cannot locate the Codex Desktop installation. Start Codex once and retry.'
        }
        $AppDirectory = Split-Path -Parent $ChatGptPath
    }

    $LauncherPath = Join-Path $AppDirectory 'Codex.exe'
    $CliPath = Join-Path $AppDirectory 'resources\codex.exe'
    foreach ($Path in @($LauncherPath, $CliPath)) {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            throw "Required Codex Desktop file not found: $Path"
        }
    }
    [pscustomobject]@{
        LauncherPath = $LauncherPath
        CliPath = $CliPath
    }
}

function Wait-SharedAppServer {
    param([int]$ReadyPort)

    $ReadyUrl = "http://127.0.0.1:$ReadyPort/readyz"
    $Deadline = (Get-Date).AddSeconds(30)
    do {
        try {
            $Response = Invoke-WebRequest -Uri $ReadyUrl -UseBasicParsing -NoProxy -TimeoutSec 2
            if ($Response.StatusCode -eq 200) {
                return
            }
        } catch {
            Start-Sleep -Milliseconds 300
        }
    } while ((Get-Date) -lt $Deadline)
    throw "Shared Codex app-server did not become ready at $ReadyUrl"
}

function Restart-CodexDesktop {
    param(
        [string]$LauncherPath,
        [string]$SharedUrl
    )

    $Processes = @(Get-Process ChatGPT -ErrorAction SilentlyContinue)
    if ($Processes.Count -gt 0) {
        $Processes | Stop-Process -Force
        Start-Sleep -Seconds 2
    }

    $StartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $StartInfo.FileName = $LauncherPath
    $StartInfo.WorkingDirectory = Split-Path -Parent $LauncherPath
    $StartInfo.UseShellExecute = $false
    if ($SharedUrl) {
        $StartInfo.Environment['CODEX_APP_SERVER_WS_URL'] = $SharedUrl
    } else {
        $StartInfo.Environment.Remove('CODEX_APP_SERVER_WS_URL')
    }
    try {
        [System.Diagnostics.Process]::Start($StartInfo) | Out-Null
    } catch {
        $StartApp = Get-StartApps |
            Where-Object { $_.AppID -match '^OpenAI\.Codex_' } |
            Select-Object -First 1
        if ($null -eq $StartApp) {
            throw
        }
        Start-Process "shell:AppsFolder\$($StartApp.AppID)"
    }
}

foreach ($Path in @($ConfigPath, $InstallPath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file not found: $Path"
    }
}

$Desktop = Resolve-CodexDesktopPaths
$OriginalConfigText = Get-Content -Raw -LiteralPath $ConfigPath
$OriginalUserWebSocketUrl = [Environment]::GetEnvironmentVariable(
    'CODEX_APP_SERVER_WS_URL',
    'User'
)
$Config = $OriginalConfigText | ConvertFrom-Json

if ($Mode -eq 'Enable') {
    if ($null -eq $Config.PSObject.Properties['codex_app_server_stdio_cli_backup']) {
        Set-ConfigProperty $Config 'codex_app_server_stdio_cli_backup' ([string]$Config.codex_cli)
    }
    Set-ConfigProperty $Config 'codex_cli' $Desktop.CliPath
    Set-ConfigProperty $Config 'codex_app_server_transport' 'desktop-shared-websocket'
    Set-ConfigProperty $Config 'codex_app_server_ws_url' $WebSocketUrl
    Set-ConfigProperty $Config 'codex_app_server_start_timeout_seconds' 30
    [Environment]::SetEnvironmentVariable('CODEX_APP_SERVER_WS_URL', $WebSocketUrl, 'User')
    $env:CODEX_APP_SERVER_WS_URL = $WebSocketUrl
} else {
    if ($Config.PSObject.Properties['codex_app_server_stdio_cli_backup']) {
        Set-ConfigProperty $Config 'codex_cli' ([string]$Config.codex_app_server_stdio_cli_backup)
    }
    Set-ConfigProperty $Config 'codex_app_server_transport' 'stdio'
    [Environment]::SetEnvironmentVariable('CODEX_APP_SERVER_WS_URL', $null, 'User')
    Remove-Item Env:CODEX_APP_SERVER_WS_URL -ErrorAction SilentlyContinue
}

try {
    $Config | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $ConfigPath -Encoding utf8
    & $InstallPath -TaskName $TaskName | Out-Host

    if ($Mode -eq 'Enable') {
        Wait-SharedAppServer -ReadyPort $Port
    }

    if (-not $NoDesktopRestart) {
        $SharedUrl = if ($Mode -eq 'Enable') { $WebSocketUrl } else { '' }
        Restart-CodexDesktop -LauncherPath $Desktop.LauncherPath -SharedUrl $SharedUrl
    }
} catch {
    $OriginalConfigText | Set-Content -LiteralPath $ConfigPath -Encoding utf8
    [Environment]::SetEnvironmentVariable(
        'CODEX_APP_SERVER_WS_URL',
        $OriginalUserWebSocketUrl,
        'User'
    )
    if ($OriginalUserWebSocketUrl) {
        $env:CODEX_APP_SERVER_WS_URL = $OriginalUserWebSocketUrl
    } else {
        Remove-Item Env:CODEX_APP_SERVER_WS_URL -ErrorAction SilentlyContinue
    }
    try {
        & $InstallPath -TaskName $TaskName | Out-Host
    } catch {
        Write-Warning "Failed to restart the original notifier configuration: $_"
    }
    if (-not $NoDesktopRestart) {
        try {
            Restart-CodexDesktop `
                -LauncherPath $Desktop.LauncherPath `
                -SharedUrl ([string]$OriginalUserWebSocketUrl)
        } catch {
            Write-Warning "Failed to reopen Codex Desktop after rollback: $_"
        }
    }
    throw
}

[pscustomobject]@{
    Mode = $Mode
    Transport = [string]$Config.codex_app_server_transport
    WebSocketUrl = $(if ($Mode -eq 'Enable') { $WebSocketUrl } else { $null })
    DesktopRestarted = -not $NoDesktopRestart
    CodexCli = [string]$Config.codex_cli
}
