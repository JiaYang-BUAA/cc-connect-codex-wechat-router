[CmdletBinding()]
param(
    [string]$TaskName = 'Codex Pinned WeChat Notifier',
    [string]$PythonPath = '',
    [string]$ConfigPath = (Join-Path (Split-Path -Parent $PSCommandPath) 'config.json')
)

$ErrorActionPreference = 'Stop'

$ProjectPath = Split-Path -Parent $PSCommandPath
$ScriptPath = Join-Path $ProjectPath 'notifier.py'

if (-not $PythonPath) {
    $PythonCommand = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($null -eq $PythonCommand) {
        $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    }
    if ($null -eq $PythonCommand) {
        throw 'Python 3.11+ was not found. Pass -PythonPath with the full path to pythonw.exe.'
    }
    $PythonPath = $PythonCommand.Source
}

foreach ($Path in @($PythonPath, $ScriptPath, $ConfigPath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file not found: $Path"
    }
}

$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Arguments = '"{0}" --config "{1}"' -f $ScriptPath, $ConfigPath
$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument $Arguments `
    -WorkingDirectory $ProjectPath
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity
$Principal = New-ScheduledTaskPrincipal `
    -UserId $Identity `
    -LogonType Interactive `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

$Task = New-ScheduledTask `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description 'Send pinned Codex Desktop answers to WeChat and route quoted replies back to the same task.'

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $ExistingTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $StopDeadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 250
        $ExistingState = (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State
    } while ($ExistingState -eq 'Running' -and (Get-Date) -lt $StopDeadline)
    Start-Sleep -Seconds 1
}

Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

$Config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
$HealthUri = "http://$($Config.router_host):$($Config.router_port)/healthz"
$Headers = @{ 'X-Codex-Quote-Token' = [string]$Config.router_token }
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
    throw "Notifier task started but health check failed: $HealthUri"
}

[pscustomobject]@{
    TaskName = $TaskName
    State = (Get-ScheduledTask -TaskName $TaskName).State
    Health = $Health.ok
    Version = $Health.version
    SubmitTransport = $Health.submit_transport
}
