![Stonkfly: a pixel fly beside a candlestick chart](assets/stonkfly.png)

# Stonkfly

A fly-connectome simulation that can operate a crypto trading account. Actual neural output, actual Coinbase integration, plus an optional OKX adapter (public market data and demo trading). Profitable learning has not been demonstrated.

**How it works:** Public Coinbase prices become an RGB chart. It stimulates 3,335 brightness inputs and 811 R8 color inputs in the retained **MaleCNS v1.0 graph: 166,700 neurons, 25.6 million connections**. A fixed neural readout proposes buy, sell or hold. A custom **Coinbase AgentKit ActionProvider** checks limits and places spot orders through Coinbase Advanced.

Positive portfolio P&L stimulates 15 identified PAM11 dopamine cells; negative P&L stimulates two PPL101 aversive dopamine cells. A candidate memory rule changes existing KC-to-MBON connections. These are engineered reinforcement signals, **not modeled pain receptors**. Synaptic changes do not establish that it learns to trade profitably. [Model and evidence](docs/model.md).

## Run it

Python 3.11 and a C++17 compiler on macOS, Linux, or Windows. Allow several GB for the dataset and dependencies; 16 GB RAM recommended.

```sh
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
python -m stonkfly prepare
python -m stonkfly run
```

### Windows with MSYS2

Install the UCRT64 C++ compiler from an **MSYS2 UCRT64** terminal:

```sh
pacman -Syu
pacman -S --needed mingw-w64-ucrt-x86_64-gcc
```

In the VS Code PowerShell terminal, make the compiler available before running Stonkfly:

```powershell
$env:Path = "C:\msys64\ucrt64\bin;$env:Path"
g++ --version
```

To add it permanently to your user `PATH` from PowerShell:

```powershell
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
[Environment]::SetEnvironmentVariable(
	"Path",
	"C:\msys64\ucrt64\bin;$userPath",
	"User"
)
```

Restart VS Code after changing the permanent Windows `PATH`. The build detects `g++` and creates `memory.dll` automatically.

This fork contains a Windows-tested baseline based on upstream PR #1. See [Windows validation](docs/windows-validation.md) for the validated environment and test evidence.

Default: **paper trades, real public BTC-USDC data, $100 simulated balance**. No key needed. Local logs, sensory images and resumable brain state go in `runs/paper/`. Ctrl-C stops it; the same command resumes.

For real orders, first create a dedicated Coinbase Advanced portfolio with **at most 100 USDC** and a portfolio-scoped **ECDSA API key with View + Trade, no Transfer**. Copy `.env.example` to `.env`, fill it in locally, then run these commands yourself:

```sh
python -m stonkfly run --live --preflight-only
python -m stonkfly run --live
```

Defaults: $10 maximum order including reserved fees, 24 filled orders/day, no shorts or leverage. A $20 drawdown stops new orders; **it does not liquidate holdings or cap further losses**. [Operation and recovery](docs/operations.md).

```sh
python -m stonkfly status
python -m stonkfly watch --out runs/paper   # read-only terminal view of the fly's decisions
python -m stonkfly serve --out runs/paper   # same, as a LAN dashboard in your browser
python -m pytest -q
```

The repo does not come funded or connected to anyone’s account. Live execution needs your local credentials and explicit opt-in.

### OKX (optional)

Read OKX public market data with the paper default, or execute against OKX Demo Trading with your own demo keys:

```sh
python -m stonkfly run --okx                        # OKX public feed, paper fills
python -m stonkfly run --okx --okx-demo --preflight-only \
    --out runs/okx-demo-accept --max-order-attempts 2
python -m stonkfly run --okx --okx-demo \
    --out runs/okx-demo-accept --max-order-attempts 2 --steps 10
```

`--okx-demo` requires `--max-order-attempts N` for anything that can submit: the attempt budget is
persisted per run directory, so a restart never resets it, and `0` means an unbounded continuous run.
OKX live trading is not supported. See [OKX adapter](docs/okx.md) for the account mode, the
balance-field model, untriggered algo-order coverage, uncertain-submission adjudication, and how the
run directories are unified into one authoritative ledger.
