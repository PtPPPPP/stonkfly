"""P1 regression guard: the launcher must never kill the worker over stderr.

Runs the PowerShell failure-injection harness (tools/test_autostart_stderr.ps1),
which drives the REAL launcher with a stub worker that writes to stderr mid-run
and must survive it. Skipped where PowerShell is unavailable (non-Windows CI).
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tools" / "test_autostart_stderr.ps1"


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell launcher test")
def test_launcher_survives_worker_stderr_and_propagates_exit_codes():
    proc = subprocess.run(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(HARNESS),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"harness failed:\n{output}"
    assert "launcher stderr tests PASSED" in output
