$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeDir = Join-Path $ProjectRoot "runtime"
$ProjectRootLower = $ProjectRoot.ToLowerInvariant()
$KiraLiveKitPath = (Join-Path $ProjectRoot "livekit\livekit-server.exe").ToLowerInvariant()
$PortsToCheck = @(3000, 7880, 8081, 8091, 8092, 5050, 5100, 5120)
$CriticalPorts = @(3000, 7880, 8081, 8091)
$KilledCount = 0
$SkippedCount = 0
$UnknownCriticalPortOwners = @()
$CleanupStartedAt = Get-Date
$CurrentPid = $PID
$CurrentParentPid = 0

function Write-CleanupLog {
    param([string]$Message)
    Write-Host $Message
}

function Get-ProcessInfoByPid {
    param([int]$ProcessId)
    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        if ($null -eq $proc) {
            return $null
        }
        return [pscustomobject]@{
            Pid = [int]$proc.ProcessId
            Name = [string]$proc.Name
            CommandLine = [string]$(if ($null -ne $proc.CommandLine) { $proc.CommandLine } else { "" })
            ExecutablePath = [string]$(if ($null -ne $proc.ExecutablePath) { $proc.ExecutablePath } else { "" })
        }
    } catch {
        return $null
    }
}

try {
    $currentProcInfo = Get-ProcessInfoByPid -ProcessId $CurrentPid
    if ($null -ne $currentProcInfo) {
        $currentProcCim = Get-CimInstance Win32_Process -Filter "ProcessId = $CurrentPid" -ErrorAction SilentlyContinue
        if ($null -ne $currentProcCim) {
            $CurrentParentPid = [int]$currentProcCim.ParentProcessId
        }
    }
} catch {
    $CurrentParentPid = 0
}

function Test-IsKiraProcess {
    param($Info)
    if ($null -eq $Info) {
        return $false
    }
    $cmd = [string]$Info.CommandLine
    $exe = [string]$Info.ExecutablePath
    $name = [string]$Info.Name
    $cmdLower = $cmd.ToLowerInvariant()
    $exeLower = $exe.ToLowerInvariant()
    $nameLower = $name.ToLowerInvariant()

    if ($cmdLower.Contains($ProjectRootLower)) { return $true }
    if ($exeLower.Contains($ProjectRootLower)) { return $true }
    if (($nameLower -eq "livekit-server.exe") -and ($exeLower -eq $KiraLiveKitPath -or $exeLower.Contains((Join-Path $ProjectRoot "livekit").ToLowerInvariant()))) { return $true }

    $markers = @(
        "kira.bat",
        "kira_app.py",
        "\agent.py",
        "whatsapp_bridge\bridge.mjs",
        "browser_agent.app",
        "vision_control_server",
        "assistant_desktop_bridge.py",
        "uvicorn"
    )
    foreach ($marker in $markers) {
        if ($cmdLower.Contains($marker.ToLowerInvariant())) {
            return $true
        }
    }
    return $false
}

function Stop-KiraProcess {
    param(
        [int]$ProcessId,
        [string]$Reason
    )
    $info = Get-ProcessInfoByPid -ProcessId $ProcessId
    if ($null -eq $info) {
        return
    }
    if ($info.Pid -eq $CurrentPid -or ($CurrentParentPid -gt 0 -and $info.Pid -eq $CurrentParentPid)) {
        $script:SkippedCount++
        Write-CleanupLog ("PREBOOT_SKIP_CURRENT_TREE pid={0} reason=current_boot" -f $info.Pid)
        Write-CleanupLog ("KIRA_BOOT_DUPLICATE_PROCESS_SKIPPED pid={0} reason=current_boot" -f $info.Pid)
        return
    }
    Write-CleanupLog ("PREBOOT_CLEANUP_PROCESS_FOUND pid={0} name={1} reason={2}" -f $info.Pid, $info.Name, $Reason)
    Write-CleanupLog ("KIRA_BOOT_DUPLICATE_PROCESS_FOUND pid={0} name={1} reason={2}" -f $info.Pid, $info.Name, $Reason)
    Write-CleanupLog ("PREBOOT_PROCESS_SCAN pid={0} name={1} reason={2}" -f $info.Pid, $info.Name, $Reason)
    if (-not (Test-IsKiraProcess -Info $info)) {
        $script:SkippedCount++
        Write-CleanupLog ("PREBOOT_PROCESS_SKIP pid={0} name={1} reason=not_kira" -f $info.Pid, $info.Name)
        Write-CleanupLog ("KIRA_BOOT_DUPLICATE_PROCESS_SKIPPED pid={0} reason=not_kira" -f $info.Pid)
        return
    }
    try {
        Stop-Process -Id $info.Pid -Force -ErrorAction Stop
        $script:KilledCount++
        Write-CleanupLog ("PREBOOT_PROCESS_KILL pid={0} name={1} reason={2}" -f $info.Pid, $info.Name, $Reason)
        Write-CleanupLog ("PREBOOT_CLEANUP_PROCESS_STOPPED pid={0} name={1} reason={2}" -f $info.Pid, $info.Name, $Reason)
        Write-CleanupLog ("KIRA_BOOT_DUPLICATE_PROCESS_KILLED pid={0} name={1} reason={2}" -f $info.Pid, $info.Name, $Reason)
    } catch {
        $script:SkippedCount++
        Write-CleanupLog ("PREBOOT_PROCESS_SKIP pid={0} name={1} reason=kill_failed:{2}" -f $info.Pid, $info.Name, $_.Exception.Message)
        Write-CleanupLog ("KIRA_BOOT_DUPLICATE_PROCESS_SKIPPED pid={0} reason=kill_failed:{1}" -f $info.Pid, $_.Exception.Message)
    }
}

function Remove-StaleRuntimeLock {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    Write-CleanupLog ("PREBOOT_CLEANUP_LOCK_SCAN path={0}" -f $Path)
    $fileName = [System.IO.Path]::GetFileName($Path)
    $content = ""
    try {
        $content = (Get-Content -LiteralPath $Path -Raw -ErrorAction Stop).Trim()
    } catch {
        $content = ""
    }
    $pidValue = 0
    if ([int]::TryParse($content, [ref]$pidValue) -and $pidValue -gt 0 -and (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)) {
        Write-CleanupLog ("PREBOOT_LOCK_SKIP path={0} reason=active_pid" -f $Path)
        Write-CleanupLog ("PREBOOT_CLEANUP_LOCK_KEPT path={0} reason=active_pid pid={1}" -f $Path, $pidValue)
        return
    }
    Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    Write-CleanupLog ("PREBOOT_CLEANUP_LOCK_REMOVED path={0} reason=stale_or_invalid" -f $Path)
    if ($fileName -like "*.pid") {
        Write-CleanupLog ("PREBOOT_STALE_PID_REMOVED path={0}" -f $Path)
    } else {
        Write-CleanupLog ("PREBOOT_STALE_LOCK_REMOVED path={0}" -f $Path)
    }
}

Write-CleanupLog "KIRA_BOOT_SINGLETON_PRECHECK_BEGIN"
Write-CleanupLog ("PREBOOT_CLEANUP_BEGIN project_root={0}" -f $ProjectRoot)
Write-CleanupLog ("PREBOOT_CLEANUP_START project_root={0}" -f $ProjectRoot)
Write-CleanupLog ("PREBOOT_CURRENT_PID pid={0}" -f $CurrentPid)

$candidatePids = @()
try {
    $candidatePids = @(Get-Process -Name python, pythonw, livekit-server, cmd -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
} catch {
    Write-CleanupLog ("PREBOOT_PROCESS_ENUM_FAILED reason={0}" -f $_.Exception.Message)
}

foreach ($candidatePid in $candidatePids) {
    $info = Get-ProcessInfoByPid -ProcessId ([int]$candidatePid)
    if ($null -eq $info) {
        continue
    }
    $cmdLower = ([string]$info.CommandLine).ToLowerInvariant()
    $exeLower = ([string]$info.ExecutablePath).ToLowerInvariant()
    if (
        $cmdLower.Contains($ProjectRootLower) -or
        # python do venv da Kira só morre se for filho do agente (multiprocessing); antes qualquer script que usasse o
        # venv (testes, ferramentas) era derrubado ao abrir a Kira
        ($exeLower.Contains($ProjectRootLower) -and $cmdLower.Contains("multiprocessing")) -or
        $cmdLower.Contains("kira_app.py") -or
        $cmdLower.Contains("kira.bat") -or
        $cmdLower.Contains("\agent.py") -or
        $cmdLower.Contains("whatsapp_bridge\bridge.mjs") -or
        $cmdLower.Contains("browser_agent.app") -or
        $cmdLower.Contains("vision_control_server") -or
        $cmdLower.Contains("assistant_desktop_bridge.py")
    ) {
        Stop-KiraProcess -ProcessId ([int]$candidatePid) -Reason "commandline_match"
    }
}

# netstat uma vez por volta (antes era uma vez por porta: 16 chamadas, ~0,4 s cada)
$netstatNow = @(netstat -ano)
foreach ($port in $PortsToCheck) {
    $lines = @($netstatNow | Select-String -Pattern (":$port\s"))
    foreach ($line in $lines) {
        $parts = ($line.ToString() -replace "\s+", " ").Trim().Split(" ")
        if ($parts.Length -lt 5) {
            continue
        }
        $targetPid = 0
        if (-not [int]::TryParse($parts[-1], [ref]$targetPid)) {
            continue
        }
        if ($targetPid -le 0) {
            continue
        }
        $info = Get-ProcessInfoByPid -ProcessId $targetPid
        if ($null -eq $info) {
            continue
        }
        Write-CleanupLog ("PREBOOT_PORT_OWNER port={0} pid={1} name={2}" -f $port, $info.Pid, $info.Name)
        if (Test-IsKiraProcess -Info $info) {
            Write-CleanupLog ("PREBOOT_PORT_KILL port={0} pid={1} reason=owned_by_kira" -f $port, $info.Pid)
            Stop-KiraProcess -ProcessId $info.Pid -Reason ("port_{0}" -f $port)
        } else {
            $script:SkippedCount++
            Write-CleanupLog ("PREBOOT_PORT_SKIP port={0} pid={1} reason=not_owned_by_kira" -f $port, $info.Pid)
            if ($port -in $CriticalPorts) {
                $script:UnknownCriticalPortOwners += [pscustomobject]@{ Port = $port; Pid = $info.Pid; Name = $info.Name }
            }
        }
    }
}

if (Test-Path -LiteralPath $RuntimeDir) {
    Get-ChildItem -LiteralPath $RuntimeDir -File -ErrorAction SilentlyContinue | Where-Object {
        $_.Extension -in @(".lock", ".pid")
    } | ForEach-Object {
        Remove-StaleRuntimeLock -Path $_.FullName
    }
}

Start-Sleep -Milliseconds 500

$criticalKiraPortsStillOwned = @()
$netstatNow = @(netstat -ano)
foreach ($port in $CriticalPorts) {
    $lines = @($netstatNow | Select-String -Pattern (":$port\s"))
    foreach ($line in $lines) {
        $parts = ($line.ToString() -replace "\s+", " ").Trim().Split(" ")
        if ($parts.Length -lt 5) {
            continue
        }
        $targetPid = 0
        if (-not [int]::TryParse($parts[-1], [ref]$targetPid)) {
            continue
        }
        $info = Get-ProcessInfoByPid -ProcessId $targetPid
        if ($null -eq $info) {
            continue
        }
        if (Test-IsKiraProcess -Info $info) {
            $criticalKiraPortsStillOwned += [pscustomobject]@{ Port = $port; Pid = $targetPid; Name = $info.Name }
        }
    }
}

$netstatFinal = @(netstat -ano)
foreach ($port in $CriticalPorts) {
    $owners = @()
    $lines = @($netstatFinal | Select-String -Pattern (":$port\s"))
    foreach ($line in $lines) {
        $parts = ($line.ToString() -replace "\s+", " ").Trim().Split(" ")
        if ($parts.Length -lt 5) { continue }
        $targetPid = 0
        if ([int]::TryParse($parts[-1], [ref]$targetPid) -and $targetPid -gt 0) {
            $owners += $targetPid
        }
    }
    if ($owners.Count -eq 0) {
        Write-CleanupLog ("PREBOOT_CLEANUP_PORT_FREE port={0}" -f $port)
    }
}

Write-CleanupLog ("PREBOOT_CLEANUP_DONE killed_count={0} skipped_count={1}" -f $KilledCount, $SkippedCount)

if ($criticalKiraPortsStillOwned.Count -gt 0) {
    foreach ($item in $criticalKiraPortsStillOwned) {
        Write-CleanupLog ("PREBOOT_CLEANUP_BLOCKED port={0} pid={1} name={2} reason=old_kira_still_running" -f $item.Port, $item.Pid, $item.Name)
    }
    exit 2
}

if ($UnknownCriticalPortOwners.Count -gt 0) {
    foreach ($item in $UnknownCriticalPortOwners) {
        Write-CleanupLog ("PREBOOT_CLEANUP_WARNING port={0} pid={1} name={2} reason=unknown_owner" -f $item.Port, $item.Pid, $item.Name)
    }
}

Write-CleanupLog "PREBOOT_CLEANUP_OK"
exit 0
