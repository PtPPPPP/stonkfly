# OKX adapter

This fork adds a first-version **OKX adapter** alongside the existing Coinbase path. It lets the
same neural control flow read OKX public market data and, optionally, execute price-bounded
fill-or-kill spot orders against **OKX Demo Trading**. The default remains Coinbase public data +
local paper execution. **OKX live trading is not supported in this build.**

Interface basis is the official OKX v5 REST API: <https://www.okx.com/docs-v5/en/>. Field semantics
below were verified against that documentation (downloaded locally), not guessed or ported from the
Coinbase SDK.

## Execution environments

Three environments are separated by construction; none can silently become another:

| CLI | mode | feed | execution | network orders |
| --- | --- | --- | --- | --- |
| `run` (default) | `paper` | `coinbase-public` | local paper fill | none |
| `run --okx` | `paper` | `okx` | local paper fill | none |
| `run --okx --okx-demo` | `okx-demo` | `okx` | OKX demo trading | simulated |
| `run --live` | `live` | `coinbase-public` | Coinbase Advanced | real |

- `--okx-demo` requires `--okx` and forbids `--fixture` / `--fast`.
- `--okx` and `--fixture` are mutually exclusive; `--live` cannot combine with `--okx`/`--okx-demo`.
- OKX **live** is rejected explicitly: `OKXBroker.from_env` refuses if `OKX_LIVE=I_ACCEPT_REAL_TRADES`.
- Public OKX market data (`--okx` without `--okx-demo`) needs no key and runs paper, like the default.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `OKX_API_KEY` | demo API key (required for `--okx-demo`) |
| `OKX_API_SECRET` | demo API secret (required) |
| `OKX_API_PASSPHRASE` | demo API passphrase (required) |
| `OKX_LIVE` | must **not** be `I_ACCEPT_REAL_TRADES`; OKX live is unsupported |

Credentials are read only from `OKXBroker.from_env` (the `--okx-demo` path). The public
`OKXMarket`/`OKXClient` path never reads or attaches a key, so a paper run cannot inherit demo
credentials. Demo requests carry `x-simulated-trading: 1` in addition to the signed auth headers.

## What was implemented

- `stonkfly/okx_client.py` — minimal HMAC-SHA256 signed v5 REST client. Signature is
  `base64(HMAC-SHA256(secret, timestamp+method+requestPath+body))`; headers
  `OK-ACCESS-KEY/SIGN/TIMESTAMP/PASSPHRASE`. Single order submission, no retry. Any failure to get
  a well-formed HTTP 200 (timeout, connection loss, non-200, malformed body) becomes an
  `OKXTransportError`, which the broker treats as an *ambiguous* outcome — never a rejection.
- `stonkfly/okx_market.py` — public observations shaped to the shared `Quote` model. Requires
  `instType=SPOT`, `quoteCcy=USDC`, `state=live` (no auto-swap to USDT if the USDC pair is missing).
  Only completed, past candles become price history. `snapshot()` refreshes the execution book but
  never appends a neural observation; `record()` does that once per tick.
- `stonkfly/okx_broker.py` — demo broker: `preflight`, `verify_balances`, `execute`, `reconcile`.
  Cash spot FOK only (`tdMode=cash`, `ordType=fok`), no leverage/contracts/shorts/transfers/withdraws.
- `stonkfly/ledger.py` — ledger identity now binds exchange/environment/account/quote currency;
  `clOrdId` is a hyphen-free 32-char hex (OKX allows ≤32 case-sensitive alphanumerics); `settle`
  handles base-currency fees, quote fees, and rebates (a rebate is never abs'd into a charge).
- `stonkfly/actions.py` / `stonkfly/broker.py` — the ActionProvider description and broker identity
  are exchange-aware (no false "Coinbase" label on the OKX path).
- `stonkfly/cli.py` — `--okx` / `--okx-demo` flags, mutual-exclusion validation, and per-mode run
  directories (`runs/paper`, `runs/okx-demo`, `runs/live`) so recovery states never mix.

## Order model

- Intent is persisted (`PREPARED`) and a legal `clOrdId` is chosen by `Ledger.reserve` **before**
  anything can be sent; the ledger flips the row to `UNKNOWN` before the request leaves the process,
  so a crash never leaves a sent order untracked.
- Response codes are handled in two layers: the top-level `code` and the per-order `sCode`. The
  **only** definite rejection is `code == "0"` with a present non-zero `sCode` — OKX's per-order
  result — which marks the intent `REJECTED`. A missing/malformed envelope, a missing `code`/`sCode`,
  or any non-zero top-level `code` (including `"1"` "Operation failed", `"50004"` timeout, `"50013"`
  system busy, `"50026"` system error — all of which OKX does not treat as confirming order
  placement) is ambiguous → `UnresolvedOrder`, keeping the intent `UNKNOWN` until reconciliation.
- Every query (`instruments`, `ticker`, `candles`, `account/config`, `balance`, `get_order`,
  `orders-pending`) validates the response envelope and raises `OKXBusinessError` on a non-zero
  `code` or malformed body — a failed `orders-pending` query never silently returns an empty list and
  lets the risk guard pass. `get_order` returns `None` only for the documented "Order does not exist"
  (`51603`) or an empty result, which the broker treats as unresolved, never as a zero fill.
- After acceptance the broker polls **only** the accepted `ordId`. A terminal state verifies identity
  (`ordId`, `clOrdId`, `instId`, `side`) and reads the actual fill (`accFillSz` × `avgPx`) plus fee.
  `ordId` alone is never treated as settled; a not-found order is never inferred as no-fill. A
  terminal order with a missing `accFillSz`, and a filled order with a missing `fee`, are incomplete
  data and stay unresolved — they are never defaulted to a zero fill/fee.
- Fees use OKX's sign convention (negative = paid, positive = rebate) in whichever currency
  (`feeCcy`/`rebateCcy` — base or quote). A fee in an unsupported currency, or a split fee/rebate
  across currencies, stays unresolved and halts.

## Recovery

- On restart, `reconcile()` re-settles any non-terminal intent: `PREPARED` (never sent) → `REJECTED`;
  `UNKNOWN` without an `ordId` is looked up by `clOrdId` (not resubmitted); `ACCEPTED` is polled to a
  terminal state. No automatic resubmission happens anywhere.
- Ledger identity (exchange/environment/account/quote currency) is stored at first run and verified
  on reopen, so a `runs/okx-demo` ledger cannot be silently reused by a paper or Coinbase run, or by
  a different OKX account. A ledger initialized before identity was recorded (identity key missing)
  is rejected on reopen instead of auto-adopting an unknown identity — a legacy demo ledger with
  historical activity is never silently backfilled, re-bound to the current account, or reconciled.
- External balance drift is an error, never treated as profit: `verify_balances()` compares exchange
  balances to the ledger at the start of each tick and again at the send boundary.

## Boundaries and limitations

- **Demo trading is not integrated here**: no demo credentials/config were supplied, so the demo path
  is exercised only through offline in-memory doubles in `tests/test_okx.py`. A real `--okx-demo`
  run requires the user to create demo keys and set the three env vars themselves.
- OKX has no quote-minimum and no quote-increment for spot, so those two `Quote` fields are `None` — a
  clear "no such rule" marker, not a `minSz × bid` or `1` placeholder. The order planner enforces only
  the real rules it has (`minSz` minimum base, `tickSz` price tick, `lotSz` base lot) and skips the
  quote-minimum check when `minimum_quote` is `None`; Coinbase's own `quote_increment`/`quote_min_size`
  rules are unchanged.
- OKX has no pre-trade fee preview comparable to Coinbase's; fees are known only after fill. The fee
  ceiling is therefore enforced at settlement: an actual fee above the previewed ceiling records the
  fill and halts further orders.
- The neural, connectome, plasticity, fixed-decoder, risk and reward semantics are unchanged. The
  risk guard may only reject an order; it never substitutes one.
