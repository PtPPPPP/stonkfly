<#
.SYNOPSIS
    Failure-injection tests for the autostart launcher's stderr handling.

.DESCRIPTION
    Drives the REAL tools\autostart_okx_demo.ps1 against a throwaway run
    directory with a stub "worker" (the venv python running -c), covering:

      Case 1  stderr written mid-run, child alive for seconds, exit 0
              -> the launcher must NOT kill the child (out2 is printed after
                 the stderr line and must appear), exit code 0 must propagate,
                 stderr text must land in the log.
      Case 2  child exits non-zero (3)
              -> the launcher must propagate 3 and log the exit line.

    Exit 0 = all checks passed; exit 1 = at least one check failed (printed).

    Run directly, or via tests/test_launcher_stderr.py.
#>
param(
    [string]$Python
)
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $Python) { $Python = Join-Path $root '.venv\Scripts\python.exe' }
$launcher = Join-Path $root 'tools\autostart_okx_demo.ps1'

$tmp = Join-Path $env:TEMP ("stonkfly-launcher-test-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null

$failures = @()
function Check {
    param([string]$Name, [bool]$Condition, [string]$Detail)
    if ($Condition) {
        Write-Host "  ok   $Name"
    } else {
        Write-Host "  FAIL $Name :: $Detail"
        $script:failures += $Name
    }
}

# The worker command override travels as ONE base64(JSON-array) token, so it
# survives every PowerShell invocation path (native argument quoting cannot
# break what contains no spaces or quotes). The stub itself is written to a
# file: PS 5.1's native join cannot carry embedded double quotes in arguments,
# and the real worker's arguments never contain any.
function EncodedArgs {
    param([string[]]$Arguments)
    $json = ConvertTo-Json -InputObject @($Arguments) -Compress
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
}

function Write-Stub {
    param([string]$Name, [string]$Code)
    $path = Join-Path $tmp $Name
    Set-Content -LiteralPath $path -Value $Code -Encoding ASCII
    return $path
}

try {
    # ---- Case 1: stderr mid-run, survival past it, exit 0 -----------------
    Write-Host 'case 1: stderr mid-run must not kill the child'
    $stub1 = Write-Stub 'stub1.py' @'
import sys, time
print("out1", flush=True)
time.sleep(1.5)
print("hello stderr", file=sys.stderr, flush=True)
time.sleep(1.5)
print("out2", flush=True)
sys.exit(0)
'@
    & powershell -NoProfile -ExecutionPolicy Bypass -File $launcher `
        -RunDir $tmp -SkipReachability `
        -WorkerCommandB64 (EncodedArgs @($stub1)) | Out-Null
    $code1 = $LASTEXITCODE
    $log1 = Get-Content -LiteralPath (Join-Path $tmp 'autostart.log') -Raw

    Check 'child exit code 0 propagated' ($code1 -eq 0) "launcher exit=$code1"
    Check 'stdout before stderr logged' ($log1 -match 'out1') 'out1 missing'
    Check 'stderr text logged, not discarded' ($log1 -match 'hello stderr') 'stderr line missing'
    # out2 is printed AFTER the stderr line: its presence proves the child
    # was not killed when stderr appeared.
    Check 'child survived stderr (out2 present)' ($log1 -match 'out2') 'child died at the stderr line'
    Check 'exit line logged' ($log1 -match 'worker exited with code 0') 'no exit line'

    # ---- Case 2: non-zero child exit propagates ---------------------------
    Write-Host 'case 2: non-zero child exit code propagates'
    $stub2 = Write-Stub 'stub2.py' @'
import sys
print("boom-out", flush=True)
print("boom-err", file=sys.stderr, flush=True)
sys.exit(3)
'@
    & powershell -NoProfile -ExecutionPolicy Bypass -File $launcher `
        -RunDir $tmp -SkipReachability `
        -WorkerCommandB64 (EncodedArgs @($stub2)) | Out-Null
    $code2 = $LASTEXITCODE
    $log2 = Get-Content -LiteralPath (Join-Path $tmp 'autostart.log') -Raw

    Check 'child exit code 3 propagated' ($code2 -eq 3) "launcher exit=$code2"
    Check 'exit line logged for failure' ($log2 -match 'worker exited with code 3') 'no exit line'
    Check 'stderr still logged on failure' ($log2 -match 'boom-err') 'stderr line missing'

    # ---- Case 3: probe failure is not masked as "held" --------------------
    Write-Host 'case 3: lock probe failure is reported, not masked'
    # A stub sitecustomize that raises SystemExit makes every python process
    # (including the lock probe) exit 7. The launcher must report
    # LOCK_PROBE_ERROR and exit 1 -- never treat it as "lock held" (exit 0).
    # The worker is never reached in this case, so the stub is safe.
    $broken = Join-Path $tmp 'broken-python'
    New-Item -ItemType Directory -Path $broken -Force | Out-Null
    $site = Join-Path $broken 'sitecustomize.py'
    Set-Content -LiteralPath $site -Value 'import sys; sys.stderr.write("probe env broken\n"); sys.exit(7)' -Encoding UTF8
    $env:PYTHONPATH = $broken
    $env:PYTHONPATH = $broken
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $launcher `
            -RunDir $tmp -SkipReachability | Out-Null
        $code3 = $LASTEXITCODE
    } finally {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    $log3 = Get-Content -LiteralPath (Join-Path $tmp 'autostart.log') -Raw
    Check 'probe failure exits 1 (not 0)' ($code3 -eq 1) "launcher exit=$code3"
    Check 'probe failure logged as LOCK_PROBE_ERROR' ($log3 -match 'LOCK_PROBE_ERROR') 'probe error not visible in log'
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

if ($failures.Count -gt 0) {
    Write-Host ("launcher stderr tests FAILED: {0}" -f ($failures -join ', '))
    exit 1
}
Write-Host 'launcher stderr tests PASSED'
exit 0
