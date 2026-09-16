# Windows validation

This fork contains a **Windows-tested baseline based on upstream PR #1**. The Windows support
implementation is that PR's work, not a reimplementation:

- Upstream repository: <https://github.com/nftechie/stonkfly>
- Upstream pull request: <https://github.com/nftechie/stonkfly/pull/1>
- Upstream Windows commit: `117d405f6bc9385277f83bb2290ccf52426d7f0a` ("Add windows support")
- Upstream base: `78ef3e05ab0fa086032098558d893667068944a0` (this PR has no divergence from `main`)

The source tree at the validated commit is byte-identical to that upstream PR commit. This
document adds observed evidence only; it changes no code. It is not an official Windows version
of Stonkfly, and it is not an upstream endorsement.

Recorded on 2026-09-16. Everything below is a real local run on one Windows machine, executed with
the default paper configuration. **No live credentials were present and no real orders were placed.**

## Environment

| Item | Value |
| --- | --- |
| OS | Windows 11 (GMT+8) |
| Python | 3.11.15 (CPython, x86-64, virtual environment at `.venv`) |
| Compiler | MSYS2 UCRT64 `g++` 16.1.0 (`C:\msys64\ucrt64\bin\g++`) |
| Shell | PowerShell 5.1 / Git Bash |
| Dependencies | `pip install -e '.[test]'` completed with exit code 0; installed versions match `pyproject.toml` exactly |

`clang++` and `cl.exe` were not on `PATH`, so the compiler probe selected `g++`. The build emitted
no `-fPIC` on Windows and produced `memory.dll`.

## MaleCNS

Rebuilt from the checksum-verified release files in `stonkfly/neural/datasets.json`:

| Check | Observed result |
| --- | --- |
| `python -m stonkfly prepare` | PASS |
| `python -m stonkfly verify` | PASS |
| Neurons | 166,700 |
| Directed edges | 25,582,938 |
| Contacts | 124,177,617 |
| `arrays_verified` | `true` |

The graph was not pruned, truncated or replaced for Windows compatibility, and no simplified
fixture was substituted for the real model. These counts match the figures recorded in
`docs/validation.md`.

## Native kernel

| Check | Observed result |
| --- | --- |
| Windows DLL build | PASS |
| Binary | `data/cache/physiology-v6/memory.dll`, 101,593 bytes |
| SHA256 | `7ad8f7216ba0d295b793e91aa37ae15d1b89cfe010f57d31b933a66bb520e6c3` |
| Recorded source SHA256 | `0c2fa74922323acb8b55309af7b720d54377de962266272f69375c6d77e40180` (matches `stonkfly/neural/kernel.cpp`) |
| Compiler | `g++` |
| Command | `g++ -O3 -std=c++17 -shared <kernel.cpp> -o <...>\memory.dll.partial` (atomic rename, no `-fPIC`) |
| `ctypes` load | PASS |
| `memory_advance` symbol | PASS |
| Linked DLLs | `KERNEL32` plus the system UCRT only |

Because the binary links no MSYS2 runtime DLL, loading it does not require the MSYS2 `bin`
directory on `PATH`.

## Tests

| Check | Observed result |
| --- | --- |
| `python -m pytest -q` | 41 passed, 1 skipped, 0 failed (exit code 0) |

The single skip is the upstream opt-in integration test that requires `STONKFLY_FULL_TEST=1`. It is
skipped by its own upstream marker, **not as a Windows workaround**. No test was deselected,
no assertion was removed, and the native kernel was not mocked to make the suite pass.

On this machine pytest's own temp-directory cleanup is intercepted by a local bulk-delete guard, so
the suite is run with `--basetemp` pointed inside the workspace. That affects only where pytest puts
its scratch files; it does not change any test outcome.

## Fixture smoke

`python -m stonkfly run --fixture --fast --steps 10 --out runs/fixture-10`

| Check | Observed result |
| --- | --- |
| Steps | 10/10 completed |
| Exit code | 0 (~54 s) |
| Decoder output | BUY x5, HOLD x5 |
| Execution | 1 FILLED, 4 VETO, 5 HOLD |
| Veto reason | `Price moved beyond neural observation tolerance` (fixture feed is time-shifted) |
| Plasticity | eligible synapses changed |
| `halted` | `null` |
| Worker shutdown | clean, no deadlock, no DLL load failure, no file-lock error |

Persisted artifacts, all present and parsed: `brain-0.npz`, `brain-1.npz`, `ledger.sqlite`,
`events.jsonl` (10 parseable JSON lines), `latest.json` (tick 10), `latest-input.png`
(320x180 RGB), `provenance.json`.

## Public market paper smoke

`python -m stonkfly run --fast --steps 10 --out runs/paper-resync`

| Check | Observed result |
| --- | --- |
| Feed | `coinbase-public`, public REST data only |
| Mode | `paper` |
| Steps | 10/10 ticks completed |
| Exit code | 0 (~69 s) |
| Decoder output | BUY x4, SELL x1, HOLD x5 |
| Execution | 1 FILLED, 4 VETO, 5 HOLD |
| `halted` | `null` |
| Live credentials | none present; `STONKFLY_LIVE` unset |
| Real orders | none |

Order-level vetoes in `--fast` mode are expected risk-control behavior, not failures. The order
cooldown is 60 seconds while `--fast` skips the wait between ticks, so repeat proposals inside that
window are correctly rejected. These order-level vetoes leave `halted` as `null`; they are distinct
from the abortive veto described below.

The filled paper order spent 9.754680320 USDC plus 0.058528081920 USDC of modeled fees, consistent
with the run recorded in `docs/validation.md`.

## Clock requirement

`stonkfly/risk.py` rejects a quote whose timestamp is more than 0.5 s ahead of the local clock or
older than `max_quote_age`. On a machine whose clock drifts, public-market mode therefore aborts on
its first tick with an abortive `Veto("Stale or future quote")` and writes `error.json`.

This was observed here: the host clock was 4.0-4.2 s behind authoritative time and the Windows Time
service was stopped. After synchronizing, the validated system reported:

| Item | Value |
| --- | --- |
| Windows Time service | Running / Automatic |
| NTP source | `time.nist.gov` |
| Measured quote age | inside the guard limits |
| Abortive vetoes after synchronizing | none (`halted = null`) |

Windows Time must be synchronized before running public-market mode. This requirement comes from the
unmodified upstream `risk.py`; it is not a Windows-specific defect and applies identically on macOS
and Linux.

## Scope and limitations

- This validates an environment, not trading performance. No profitable policy is demonstrated, and
  the repeated one-sided BUY proposals documented in `docs/validation.md` are not addressed here.
- Paper mode only. Live trading was not enabled, and `STONKFLY_LIVE` was never set.
- The neural, connectome, plasticity, decoder, risk and trading semantics are unchanged from
  upstream PR #1.
- Windows CI is not part of this validation. Upstream's workflow runs on `ubuntu-latest` only.

## Status

```text
WINDOWS_BASELINE_READY=true
```
