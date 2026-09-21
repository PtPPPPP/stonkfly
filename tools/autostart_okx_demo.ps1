<#
.SYNOPSIS
    Unattended launcher for the continuous OKX demo worker (Stonkfly).

.DESCRIPTION
    Starts the continuous OKX demo run in runs\okx-authoritative-staging, but
    only once the exchange is actually reachable through the local HTTP proxy.

    Why the gate exists: starting the worker against an unreachable exchange
    could halt an otherwise healthy run. This script therefore waits for
    reachability and refuses instead of starting.

    It also respects an explicit STOP file and never starts a second worker:
    the run directory's operating-system lock makes the CLI refuse on its own,
    and the lock probe here distinguishes "held" (nothing to do) from "probe
    failed" (refuse loudly, never mask an environment failure as success).

    Nothing here changes ledger money state, attempt counters or the reward
    anchor; it only decides whether a worker may start.

    Worker output: stdout and stderr both land in autostart.log line by line.
    The child is started with $ErrorActionPreference scoped to Continue and
    each line is appended individually, because Windows PowerShell 5.1 turns
    the first stderr line of a native command under 2>&1 into a terminating
    NativeCommandError when EAP=Stop -- which killed the worker mid-run. No
    log handle is held between lines, so log rotation stays possible while a
    continuous worker runs.

.OUTPUTS
    Exit code 0  = the worker ran and exited, or there was nothing to do.
    Exit code 1  = preconditions were not met; nothing was started.
    Otherwise    = the worker's own exit code, propagated unchanged.

.PARAMETER ProbeOnly
    Run the safety checks and report, without starting the worker.

.PARAMETER WorkerCommandB64
    Test/diagnostic hook: UTF-8 base64 of a JSON array replacing the worker
    command line (arguments to the venv python). One token by design, so it
    survives every PowerShell invocation path unharmed. Never used by the
    scheduler.

.PARAMETER SkipReachability
    Test/diagnostic hook: skips the OKX reachability gate. Never used by the
    scheduler.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\autostart_okx_demo.ps1 -ProbeOnly
#>
[CmdletBinding()]
param(
    [string]$RunDir,
    [string]$Proxy = 'http://127.0.0.1:7890',
    [int]$ProxyTimeoutSeconds = 900,
    [int]$PollSeconds = 15,
    [switch]$ProbeOnly,
    [string]$WorkerCommandB64,
    [switch]$SkipReachability
)

$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not $RunDir) { $RunDir = Join-Path $root 'runs\okx-authoritative-staging' }

if (-not (Test-Path -LiteralPath $RunDir)) {
    Write-Host "run directory not found: $RunDir"
    exit 1
}
$RunDir = (Resolve-Path -LiteralPath $RunDir).Path
$log = Join-Path $RunDir 'autostart.log'

function Write-Log {
    param([string]$Message)
    $line = '{0} {1}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $Message
    try { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 } catch { }
    Write-Host $line
}

function Test-ExchangeReachable {
    $env:HTTPS_PROXY = $Proxy
    $env:HTTP_PROXY = $Proxy
    $env:NO_PROXY = 'localhost,127.0.0.1,::1'
    $probe = "from stonkfly.okx_client import OKXClient; r = OKXClient(timeout=10).request('GET', '/api/v5/public/time'); raise SystemExit(0 if r.get('code') == '0' else 1)"
    Push-Location $root
    $prev = $ErrorActionPreference
    try {
        # EAP=Continue here: a probe that writes to stderr must be read as
        # "not reachable yet" (retry), not crash the launcher.
        $ErrorActionPreference = 'Continue'
        & $python -c $probe *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $prev
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "python not found: $python"
    exit 1
}

function Get-WorkerLockState {
    # The run directory's lock is an operating-system lock, not the file's
    # existence: it is released automatically when the owning process dies, so
    # a leftover worker.lock is harmless. The probe's exit codes are a contract:
    #   0 = free (probe acquired and released the lock)
    #   2 = held (another worker owns the directory)
    #   anything else = the probe itself failed (broken venv, import error,
    #       filesystem trouble) -- reported as LOCK_PROBE_ERROR, never masked
    #       as "held", because that would silently prevent every future start
    #       while the log claims all is well.
    $probe = @"
import sys
from pathlib import Path
from stonkfly import locking
try:
    handle = locking.acquire(Path(r'$RunDir') / 'worker.lock')
except BlockingIOError:
    sys.exit(2)
except BaseException:
    sys.exit(3)
else:
    locking.release(handle)
    sys.exit(0)
"@
    Push-Location $root
    $prev = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $probeOutput = & $python -c $probe 2>&1
        $code = $LASTEXITCODE
    } catch {
        $code = -1
        $probeOutput = @($_ | ForEach-Object { "$_" })
    } finally {
        $ErrorActionPreference = $prev
        Pop-Location
    }
    if ($code -eq 0) { return 'free' }
    if ($code -eq 2) { return 'held' }
    $lines = @($probeOutput | ForEach-Object { "$_" } | Where-Object { $_ })
    Write-Log ("LOCK_PROBE_ERROR: lock probe failed (exit {0}): {1}" -f $code, (($lines | Select-Object -First 3) -join ' | '))
    return 'error'
}

# 1. An explicit STOP wins over any automatic start.
if (Test-Path -LiteralPath (Join-Path $RunDir 'STOP')) {
    Write-Log 'STOP file present; not starting (delete STOP to allow a restart)'
    exit 0
}

# 2. Never start while the exchange is unreachable.
if (-not $SkipReachability) {
    $deadline = (Get-Date).AddSeconds($ProxyTimeoutSeconds)
    Write-Log "waiting for OKX through $Proxy (up to $ProxyTimeoutSeconds s)"
    while (-not (Test-ExchangeReachable)) {
        if ((Get-Date) -ge $deadline) {
            Write-Log 'OKX still unreachable; nothing was started and the ledger was not halted'
            exit 1
        }
        Start-Sleep -Seconds $PollSeconds
    }
    Write-Log 'OKX reachable through the proxy'
}

if ($ProbeOnly) {
    Write-Log 'probe only: safety checks passed, worker not started'
    exit 0
}

# 3. Nothing to do when a worker already owns the directory; a probe failure
#    refuses loudly instead of masquerading as "nothing to do".
$lockState = Get-WorkerLockState
if ($lockState -eq 'held') {
    Write-Log 'a worker already owns this run directory; nothing to do'
    exit 0
}
if ($lockState -ne 'free') {
    Write-Log 'lock probe failed; not starting. Fix the environment; the next launch retries.'
    exit 1
}

# 4. Keep the launcher log bounded (one rotated generation). Attempted on
#    every launch: the worker's output is appended line by line, so rotation
#    can succeed even mid-run. If the file is momentarily busy the failure is
#    logged and the log simply keeps growing until a later attempt.
if ((Test-Path -LiteralPath $log) -and ((Get-Item -LiteralPath $log).Length -gt 2MB)) {
    try {
        Move-Item -LiteralPath $log -Destination "$log.1" -Force -ErrorAction Stop
        Write-Log 'rotated autostart.log to autostart.log.1'
    } catch {
        Write-Log ("log rotation failed (log left in place): {0}" -f $_.Exception.Message)
    }
}

# 5. Run the worker in the foreground, so the scheduler tracks its lifetime.
#    stdout and stderr are appended to the log line by line. The child's exit
#    code propagates unchanged; the launcher's own failures still fail stop.
if ($WorkerCommandB64) {
    $workerArgs = @(
        [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($WorkerCommandB64)) |
            ConvertFrom-Json
    )
} else {
    $workerArgs = @(
        '-m', 'stonkfly', 'run', '--okx', '--okx-demo',
        '--max-order-attempts', '0', '--out', $RunDir,
        '--reward-anchor', 'trade', '--reward-horizon-ticks', '30',
        '--show-portfolio-state'
    )
}
Write-Log "starting worker: $($workerArgs -join ' ')"
Push-Location $root
$prevEap = $ErrorActionPreference
try {
    # EAP=Continue is scoped to exactly this pipeline. Under EAP=Stop,
    # PowerShell 5.1 converts the child's first stderr line into a terminating
    # NativeCommandError, which tears down the pipeline and kills the worker.
    # stderr lines are flattened to their message text and appended individually,
    # so no log handle is held open between lines.
    $ErrorActionPreference = 'Continue'
    & $python @workerArgs 2>&1 | ForEach-Object {
        Add-Content -LiteralPath $log -Value ("$_") -Encoding UTF8
    }
    $code = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevEap
    Pop-Location
}
Write-Log "worker exited with code $code"
exit $code
