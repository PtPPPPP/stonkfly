# OKX adapter

This fork adds a first-version **OKX adapter** alongside the existing Coinbase path. It lets the
same neural control flow read OKX public market data and, optionally, execute price-bounded
fill-or-kill spot orders against **OKX Demo Trading**. The default remains Coinbase public data +
local paper execution. **OKX live trading is not supported in this build.**

Interface basis is the official OKX v5 REST API: <https://www.okx.com/docs-v5/en/>. Field semantics
below were verified against that documentation and against read-only probes of the demo account, not
guessed or ported from the Coinbase SDK.

## Execution environments

Three environments are separated by construction; none can silently become another:

| CLI | mode | feed | execution | network orders |
| --- | --- | --- | --- | --- |
| `run` (default) | `paper` | `coinbase-public` | local paper fill | none |
| `run --okx` | `paper` | `okx` | local paper fill | none |
| `run --okx --okx-demo` | `okx-demo` | `okx` | OKX demo trading | simulated |
| `run --live` | `live` | `coinbase-public` | Coinbase Advanced | real |

- `--okx-demo` requires `--okx`, forbids `--fixture` / `--fast`, and **requires an explicit
  `--max-order-attempts N`** (see *Bounded run vs continuous operation*).
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

## Commands

The **formal** preflight, the automatic run, status and recovery all use the same configuration and
the same persistent ledger. `tools/okx_diagnose.py` is a *diagnostic* that uses a throwaway ledger:
it is not a substitute for any of these.

```sh
# Formal read-only preflight (creates/verifies the persistent run ledger; sends no order)
python -m stonkfly run --okx --okx-demo --preflight-only \
    --out runs/okx-demo-accept --max-order-attempts 2

# Bounded acceptance run: 10 observations, at most 2 submission attempts
python -m stonkfly run --okx --okx-demo \
    --out runs/okx-demo-accept --max-order-attempts 2 --steps 10

# Stop, inspect, resume (same directory resumes budget, inventory, counter, checkpoint)
touch runs/okx-demo-accept/STOP
python -m stonkfly status --out runs/okx-demo-accept
python -m stonkfly run --okx --okx-demo \
    --out runs/okx-demo-accept --max-order-attempts 2 --steps 10

# If the run stopped on an uncertain submission, see Recovery and
# tools/okx_unknown_audit.py.

# After an intentional configuration or source change (see Protocol migration)
python -m stonkfly run --okx --okx-demo --out DIR --migrate-protocol --preflight-only
```

The single authoritative run directory is `runs/okx-authoritative-staging` once the prepared
migration has been reviewed and promoted; see Ledger unification.

### Unattended start

`tools/autostart_okx_demo.ps1` starts the continuous run unattended; the Windows scheduled task
`Stonkfly OKX Demo Worker` invokes it at logon (after one minute) and repeats it every 15 minutes as
a watchdog. Two gates make that safe:

- **It refuses to start until the exchange is reachable through the local proxy.** A failure raised
  after the directory has already done work halts the ledger, and a halted ledger refuses every
  future order until a human clears it — so starting the worker while the proxy is still coming up
  at logon could halt a healthy run. The launcher waits and then refuses instead; the repeating
  trigger starts it once the proxy appears.
- **It does nothing when a `STOP` file is present or a worker already owns the directory.** Ownership
  is the operating-system lock, not the file's existence, so the repeating trigger cannot start a
  second worker even though `worker.lock` is always there.

It appends to `runs/okx-authoritative-staging/autostart.log` (kept to one rotated generation) and
changes no ledger money state, attempt counter or reward anchor. Passing `-ProbeOnly` runs the safety
checks and reports without starting anything. Delete the task with
`Unregister-ScheduledTask -TaskName 'Stonkfly OKX Demo Worker'`.

## Account model

- **Account mode.** Current OKX documentation defines `acctLv` 1 as Spot mode, 2 as Futures mode,
  3 as Multi-currency margin and 4 as Portfolio margin. Both 1 and 2 accept `tdMode=cash` SPOT
  orders, so either may run this adapter, but only while the account provably holds no margin,
  derivative or borrow exposure. Levels 3 and 4 are rejected outright. (This corrects an earlier
  assumption that `acctLv=2` was a cash-only "single-currency margin" mode.)
- **Identity.** The API key must carry `trade` and must not carry `withdraw`; the account must be a
  main account (`type=0`, `uid == mainUid`). The account id is bound into the ledger and never
  printed.
- **Borrowing.** `enableSpotBorrow`, `autoLoan` and `spotBorrowAutoRepay` must all be *present* and
  `false`. A missing setting cannot be verified, so it stops the run.
- **Budget.** The demo account is not assumed to be empty. At first init a snapshot of its actual
  cash balances (`baseline`) is persisted and the bot is granted a virtual `budget` (default 100
  quote units). The quote budget must be covered by **`availBal`**, not merely by `cashBal`: a
  balance that is displayed but frozen, order-frozen or lent out cannot fund an order.
- **Unallocated assets.** Gifted BTC/ETH/OKB and the quote balance beyond the budget must not move.
  The bot may only sell base units it bought itself, and a SELL leaves room for a base-currency fee
  (see *Order model*). Reconciliation compares exchange cash against
  `baseline + (cash - budget)` and `baseline + positions`, so a deposit, withdrawal or third-party
  trade is a hard error rather than profit.

## Balance-detail field model

`GET /api/v5/account/balance` details are read against a documented model instead of "any non-zero
field is bad". Per-field descriptions and the *"Distribution of applicable fields under each account
level"* matrix are the basis; the encodings below were confirmed against a live read-only sample.

| Group | Fields | Rule |
| --- | --- | --- |
| Required balances | `cashBal`, `availBal` | must be present, parseable and non-negative — never defaulted to zero |
| Risk indicators | `frozenBal`, `ordFrozen`, `liab`, `crossLiab`, `isoLiab`, `interest`, `upl`, `uplLiab`, `isoEq`, `isoUpl`, `imr`, `mmr`, `notionalLever`, `twap`, `frpType`, `stgyEq`, `borrowFroz`, `spotInUseAmt`, `clSpotInUseAmt`, `maxSpotInUse`, `fixedBal`, `autoLendAmt`, `autoLendMtAmt` | must be present; a present `""` means "not applicable under this account level" and a present `0` means clear; anything else is a hard stop. A **missing** field is a hard stop — the risk cannot then be verified |
| Quantitative | `maxLoan`, `mgnRatio`, `eq`, `eqUsd`, `availEq`, `disEq`, `rewardBal`, `colRes`, `colBorrAutoConversion` | range-checked when present, **not** forced to zero |
| Local feature state | `autoLendStatus`, `autoStakingStatus` | rejected when `pending`/`active` |

`maxLoan` is a maximum *borrowable* amount and `mgnRatio` is a *ratio*: requiring either to be zero
would be meaningless, and a positive value on an account with no positions is not exposure. `upl`,
`imr`, `mmr`, `isoEq` and `notionalLever` are different — a non-zero value there *does* indicate
margin or derivative exposure.

Account mode, key permissions, identity and the whole risk set are re-verified on every tick. The
send boundary re-verifies a narrower, order-relevant subset — mode, identity, the funds the order
needs and the resting orders OKX will list — because each OKX request costs about a second and the
plan must reach the exchange inside the 15 s quote-age window. An untriggered algo order cannot move
funds until it fires, and firing changes balances, which the boundary reconciliation catches.

## Order model

- Intent is persisted (`PREPARED`) and a legal `clOrdId` is chosen by `Ledger.reserve` **before**
  anything can be sent. At the send boundary `Ledger.begin_attempt` spends one attempt from the
  persisted budget and flips the row to `UNKNOWN` **in the same transaction**, before the request can
  leave the process, so a crash after that point over-counts rather than repeating a submission.
- Response codes are handled by the documented authority rule (OKX General Information: "It is
  `sCode` and `sMsg` that represent the request result or error reason when the return data has
  `sCode` rather than `code` and `msg`"). When a response item carries an `sCode`, that per-order
  result decides: non-zero → a **definite** rejection (`REJECTED`, with the top-level `code` and the
  `sCode` recorded in the run's `events.jsonl` — they name the exchange's own reason without echoing
  a body); zero → accepted and polled by `ordId`. This matters in practice: an envelope-level failure
  (`"1"` "Operation failed", `"2"` partial) that still carries an `sCode` is an answered rejection,
  and reading it as ambiguous would halt the run and demand a human adjudication for something the
  exchange already answered. A missing/malformed envelope, or a response with **no** usable `sCode`
  (a `"50004"` timeout or `"50013"` busy envelope carries none) is ambiguous → `UnresolvedOrder`,
  keeping the intent `UNKNOWN` until reconciliation, with the top-level `code` attached to
  `error.json` for diagnosis.
- Every query (`instruments`, `ticker`, `candles`, `account/config`, `balance`, `positions`,
  `account-position-risk`, `orders-pending`, `orders-algo-pending`, `get_order`) validates the
  response envelope and raises `OKXBusinessError` on a non-zero `code` or malformed body — a failed
  query never silently returns an empty list and lets the risk guard pass. `get_order` returns `None`
  only for the documented "Order does not exist" (`51603`) or an empty result, which the broker
  treats as unresolved, never as a zero fill.
- After acceptance the broker polls **only** the accepted `ordId`. A terminal state verifies identity
  (`ordId`, `clOrdId`, `instId`, `side`) and reads the actual fill (`accFillSz` × `avgPx`) plus fee.
  `ordId` alone is never treated as settled; a not-found order is never inferred as no-fill. A
  terminal order with a missing `accFillSz`, and a filled order with a missing `fee`, are incomplete
  data and stay unresolved — they are never defaulted to a zero fill/fee.
- Fees use OKX's sign convention (negative = paid, positive = rebate) in whichever currency
  (`feeCcy`/`rebateCcy` — base or quote). A fee in an unsupported currency, or a split fee/rebate
  across currencies, stays unresolved and halts.
- **Sell price and OKX's dynamic price band.** OKX rejects a sell order priced below a per-instrument
  band (observed as `sCode 51138`, *"The lowest price limit for sell orders is {param0}."*; the mirror
  code for buys is `51137`). The band sits at about −0.5% from last, and because bid ≤ last and the
  limit is rounded down to the tick, a sell buffer equal to the band lands on or under the line by
  construction — every sell this bot priced at `bid × (1 − 0.005)` was rejected (7 of 7), while every
  buy at +0.5% passed, because rounding up keeps it on the safe side. The sell buffer is therefore
  capped at 0.2% (`_SELL_PRICE_BAND_SAFETY` in `risk.py`): the FOK sell still fills at the bid of the
  moment — the limit is only the floor the exchange will accept — and the cost is that a >0.2% drop
  between observation and submission cancels the order (zero fill, no harm) instead of filling it.
  Because the band is dynamic and undisclosed, every order also carries the documented
  `pxAmendType=1`: a price outside the band is amended to the band's edge instead of rejected. With
  both defenses live, sells filled and settled (previously 0 for 7, then 0 for 2 with UNKNOWN halts).
- **Sell sizing.** A SELL may consume only the bot's own inventory and leaves room for a
  base-currency fee reserve: `size ≤ held / (1 + fee_reserve)`. Without that reserve, selling the
  whole holding would be settled *after* the fill with a base fee deducted from a position that no
  longer covers it, and would also be the size at which the exchange could reach into the gifted
  base the account happens to hold. A holding sitting exactly at the exchange minimum therefore has
  no legal SELL size and is vetoed rather than oversold. The buy side keeps its quote reserve for a
  fee charged in the quote currency.

## Read-only retries

Read-only queries retry OKX's transient codes (`51054`, `50004`, `50013`, `50026`) a bounded number
of times (3, with backoff) and then raise. Exhausting the retries is an error, never an empty
result. Order placement never goes through that path: `POST /api/v5/trade/order` is issued exactly
once per attempt, and an ambiguous outcome is reconciled rather than resent.

## Algo / strategy order coverage

Untriggered conditional and strategy orders can move funds later, so every `ordType` OKX documents
for the account mode is covered: `conditional`, `oco`, `trigger`, `move_order_stop`, `iceberg`,
`twap`, `smart_iceberg`, plus `chase` when the mode permits futures/swap instruments (`acctLv`
2/3/4). `chase` is documented as FUTURES/SWAP-only, which is the one ordType with a documented
reason to be inapplicable to `acctLv=1` (Spot mode).

### Which endpoint is authoritative, and why

Coverage comes from **`GET /api/v5/trade/orders-algo-pending`**, and from nothing else. Its
documented purpose is exactly this: *"Retrieve a list of untriggered Algo orders under the current
account"* / *"获取当前账户下未触发的策略委托单列表"*.

The state names matter, and the Chinese is what settles them:

| state | Chinese | literal | where it appears |
| --- | --- | --- | --- |
| `live` | 待生效 | "awaiting effect" | `orders-algo-pending` and `order` details |
| `pause` | 暂停生效 | "effect suspended" | `orders-algo-pending` and `order` details |
| `partially_effective` | 部分生效 | "partially took effect" | `order` details, WS channels |
| `effective` | **已生效** | "**has already taken effect**" | `orders-algo-history`, `order` details, WS |
| `canceled` | 已撤销 | "has been cancelled" | `orders-algo-history`, `order` details, WS |
| `order_failed` | 委托失败 | "the order failed" | `orders-algo-history`, `order` details, WS |
| `partially_failed` | 部分委托失败 | "partially failed" | `orders-algo-history`, `order` details |

`live`/`pause` are the not-yet-triggered states, and those are precisely what `orders-algo-pending`
returns. `effective` is the **opposite**: 已生效 is the perfective counterpart of 待生效, and the
documentation's own example of an `effective` order carries a populated `triggerTime`, `ordId`,
`ordIdList` and `actualSz` — i.e. an order that has already triggered and spawned a real order.

**`orders-algo-history` is therefore not a view of untriggered orders, and is not used as one.** Its
`state` filter accepts only `effective`, `canceled` and `order_failed`; `live` and `pause` are
neither filterable nor returned there. An empty history result says nothing about untriggered
orders. *(An earlier revision of this adapter read `effective` as "not yet triggered", used the
history view as coverage, and reported that as verified. That was wrong in both directions: the
wrong endpoint, and a state whose documented meaning is the reverse. The code has no such call, and
`tests/test_okx_risk_model.py::test_the_history_effective_view_is_not_a_substitute` guards against
reintroducing it.)*

### Availability, and what happens when a query fails

`51054` is documented as *"Request timed out. Please try again."* (HTTP 500). It is **not** a "this
feature does not exist" code — those are `50038` (unavailable in demo trading) and `51010`
(unsupported under the current account mode). Read-only queries retry it a bounded number of times.

This endpoint's availability is not constant:

| observation | result |
| --- | --- |
| 2026-09-16, read-only | `conditional`/`oco` answered `code=0`; `trigger`, `move_order_stop`, `iceberg`, `twap`, `smart_iceberg`, `chase` answered `51054` persistently — 6 spaced retries, `instType`/`instId` narrowing, and both `www.okx.com` and `openapi.okx.com` |
| 2026-09-17, read-only | **all eight** answered `code=0` with an empty list, 32/32 queries over 4 rounds |

Because availability can lapse, the check **fails closed**: a type that does not return a definitive
`code=0` list is reported as unverified, named with its OKX code, and the run stops. There is no
acknowledgement parameter and no substitute view. A `verified` result therefore means "this check
got a definitive empty untriggered listing for every applicable ordType", not "coverage can never
fail". What is *not* claimed anywhere: that a failed query means there are no orders, or that any
other endpoint can stand in for this one.

### Result

`preflight` reports the source and the per-type outcome:

```
algo_coverage: verified
algo_coverage_source: orders-algo-pending (documented untriggered listing)
algo_coverage_types_verified: [conditional, oco, trigger, move_order_stop, iceberg, twap, smart_iceberg, chase]
```

When a type cannot be listed the run stops with the offending types and codes, for example
`trigger(code=51054)`, and `tools/okx_diagnose.py` reports the same as
`error_category: algo_coverage_unverified`. Nothing turns that into permission to proceed.

## Account and position risk

`/api/v5/account/risk-state` is documented as **Portfolio-margin-only** and answers `51010` for this
account, so the applicable snapshot is `/api/v5/account/account-position-risk` for `MARGIN`, `SWAP`
and `FUTURES`: `posData` must be present and empty, and `adjEq` must be present and empty (`adjEq` is
reported only for multi-currency/portfolio margin). A missing field is a stop, never a silent "no
risk". `/api/v5/account/max-loan` returns HTTP 400 for this account, so `maxLoan` is read from the
balance details instead.

## Bounded run vs continuous operation

These are separate modes and the CLI forces the choice to be explicit:

- **Bounded acceptance** — `--max-order-attempts N` with `N > 0`. The count is persisted per run
  directory and is never reset by a restart, so `--steps 10 --max-order-attempts 2` can submit at
  most twice no matter how often it is restarted. Definite rejections and lost responses both spend
  an attempt. When the budget runs out the worker stops on its own (no STOP file and nobody watching)
  after reconciling, and no new submission is possible.
- **Continuous operation** — `--max-order-attempts 0` means unbounded, and may only start from a
  fully resolved state: algorithm coverage verified, no unresolved intent, no halt.

`--max-order-attempts` is required with `--okx-demo` for anything that could submit, so a bounded
acceptance run can never be mistaken for a continuous one. A read-only `--preflight-only` does not
need it, so an account can be checked without also binding a future run's policy.

Reopening a directory with a different value is refused unless
`--allow-attempt-limit-change` is passed, and changing it never resets the attempts already spent.
A directory written before this counter existed inherits a starting count equal to its existing
order rows, i.e. it over-counts rather than under-counts.

This budget is deliberately *not* `daily_orders`: `daily_orders` is a rolling rate limit (24/day,
>=60 s apart), while `--max-order-attempts` is the total a run directory may ever submit.

## Recovery

- On restart, `reconcile()` re-settles any non-terminal intent: `PREPARED` (never sent) → `REJECTED`;
  `UNKNOWN` without an `ordId` is looked up by `clOrdId` (not resubmitted); `ACCEPTED` is polled to a
  terminal state. No automatic resubmission happens anywhere. Reconciliation still runs when the
  attempt budget is exhausted, so a submitted order can always be resolved.
- **An UNKNOWN intent stops the worker permanently, by design.** There is no automatic way to tell
  "never reached the exchange" from "filled but unreported", so the bot refuses to guess. Normal
  start-up, `reconcile` and `--resume-reviewed` all leave it open — a query that finds nothing is not
  evidence that no order exists.
- **Investigating is read-only and separate from deciding.** `tools/okx_unknown_audit.py DIR` collects
  the evidence for every open UNKNOWN intent and prints it, and it cannot change a ledger. Closing one
  is a separate, explicitly named human adjudication:

  ```sh
  python -m stonkfly run --okx --okx-demo --out DIR --adjudicate-absent CLORDID --resume-reviewed
  ```

  An adjudication is only accepted when all of the following hold, in this order:

  1. the account identity, mode and balance reconciliation still pass — if any funds moved, the intent
     stays UNKNOWN rather than being closed over a real fill;
  2. the intent is still UNKNOWN with no exchange order id, re-checked inside the transaction;
  3. the intent is a `fok` order, which OKX defines as all-or-nothing, so "no fill" is unambiguous;
  4. the read-only investigation found no match in the order lookup, the open orders, a *complete*
     paged 7-day order history, or a *complete* paged 3-month archive.

  The status change and its audit record are committed in one transaction, so a crash cannot close an
  intent without recording why. The record names its source and states plainly that it is
  `human_adjudication_not_exchange_confirmation`. Consumed attempts are never returned and nothing is
  ever resubmitted.
- **What the retention windows mean.** OKX keeps orders "canceled without any fills" for only
  **2 hours** in `orders-history`, while fills stay visible there for 7 days and in the archive for
  3 months. So the age of the intent decides what a clean miss rules out, and the recorded basis says
  which case applied: within 2 hours a canceled-unfilled order would still be listed, so both
  possibilities are ruled out; after that only a fill is ruled out — and a canceled-without-fill order
  moves no cash, position or fee, which is why it cannot affect the accounting.
- **Earlier resolutions are history, not precedent.** `resolved_orders` entries written before this
  mechanism lack a recorded basis; the audit tool flags them (`evidence_recorded: false`) and their
  evidence cannot be re-derived, because the 2-hour canceled-unfilled window has long passed.
- Ledger identity (exchange/environment/account/quote currency) is stored at first run and verified
  on reopen, so a `runs/okx-demo` ledger cannot be silently reused by a paper or Coinbase run, or by
  a different OKX account. A ledger initialized before identity was recorded (identity key missing)
  is rejected on reopen instead of auto-adopting an unknown identity — a legacy demo ledger with
  historical activity is never silently backfilled, re-bound to the current account, or reconciled.
- A run directory bound to different settings or source is refused rather than migrated: use a new
  directory for a new protocol, and leave old directories in place rather than deleting them or
  re-carving a budget out of them.
- External balance drift is an error, never treated as profit: `verify_balances()` compares exchange
  balances to the ledger at the start of each tick and again at the send boundary.

## Protocol migration

A run directory is bound to the source revision and protocol that created it. When they change, the
run refuses with "Run source/protocol changed" rather than migrating itself. Adopting the new
signature in place is an explicit, recorded step:

```sh
python -m stonkfly run --okx --okx-demo --out DIR --migrate-protocol --preflight-only
```

`Ledger.migrate_protocol` appends the previous signature, the new one and the time to
`protocol_migrations`; the trail is shown by `status`. Cash, positions, baseline, budget, consumed
attempts and the checkpoint are all left exactly as they were — a migration can never hand back
budget or submission attempts, and it skips no risk check, because the full preflight still runs
against the new source. This exists so a legitimate configuration change does not brick a directory
permanently; the alternative, ignoring the hash check, is not offered.

## Ledger unification

Several run directories traded against the same demo account, so the accounting is split. Three
read-only tools and one migration step handle that. None of them deletes, clears or overwrites an
existing ledger.

| Tool | Purpose |
| --- | --- |
| `python -m stonkfly run --okx --okx-demo --preflight-only --out DIR` | **formal** preflight against the persistent ledger (adds `--max-order-attempts N` for anything that can submit) |
| `python -m stonkfly status --out DIR` | local run state: tick, cash, positions, unresolved orders, attempt budget, resolutions, protocol migrations, last coverage. No account id |
| `tools/okx_probe.py HOST:PORT` | one unauthenticated public request, to check connectivity |
| `tools/okx_diagnose.py HOST:PORT` | diagnostic preflight on a throwaway ledger; stable error categories and business/HTTP codes only |
| `tools/okx_unknown_audit.py DIR` | read-only evidence for open UNKNOWN intents; cannot change a ledger |
| `tools/okx_ledger_audit.py` | read-only audit of every run directory plus the exchange's own record |
| `tools/okx_ledger_migrate.py` | build the unified ledger in a staging directory and verify it |
| `tools/okx_ledger_migrate.py --check-promotion` | report whether promotion would be allowed; changes nothing |
| `tools/okx_ledger_migrate.py --repair-checkpoint` | copy a missing checkpoint into a prepared directory (file + audit only) |
| `tools/okx_ledger_migrate.py --confirm-promotion` | promote, atomically, only if every check passes |
| `tools/fly_pretrain.py` | offline pretraining: the same fly explores historical candles in a paper ledger; produces a deployable checkpoint. Never trades, never claims validated profitable learning |

### What the audit establishes

`tools/okx_ledger_audit.py` writes a Git-ignored report under `runs/audit/` (the terminal summary is
sanitised; exact identifiers stay in that file) and answers, from local ledgers plus read-only
exchange queries:

- every exchange-backed directory describes the **same account, environment, quote currency and
  protocol**;
- each directory's recorded settlement **matches the exchange's own record** (`exchange_match: 8/8`
  on this account), with no duplicate bookings and no conflicting settlements;
- the directories' timelines are ordered from in-ledger evidence (not filenames or mtimes) with **no
  overlap**, so there was no concurrent run;
- **every directory created after the first absorbed the buys of the ones before it into its
  "gifted" baseline** — `absorbed_prior_bot_base: true`. No later snapshot is a valid gift;
- the account had **prior activity of its own** before this project (8 filled orders with `LC…`
  client ids dated weeks earlier, none during bot activity), so even the earliest baseline is "the
  earliest snapshot we have", not a pristine gift;
- conservation holds exactly: `gift + bot contribution == the live account`, delta 0 for every
  currency, with the untouched currencies unchanged.

### What the migration does

`tools/okx_ledger_migrate.py` writes to a **new** directory (default
`runs/okx-authoritative-staging`) and refuses if one already holds a ledger, so it cannot overwrite
or reset anything. Its rules follow the audit's findings:

1. **The gift is derived, not adopted.** `gift = the live account - everything the bot is known to
   have done`. Adopting any directory's snapshot would silently reclassify bot inventory as a gift,
   which the audit shows every later directory already did.
2. **The earliest recorded baseline is the cross-check**, not the source: if the derived gift
   disagrees with it, bot activity is missing from the union and the migration refuses. On this
   account the two agree to the last digit for all four currencies.
3. **One exchange order is counted once**, deduplicated by exchange order id, with any conflicting
   settlement between directories reported and refused.
4. **Nothing is regenerated.** `budget` stays 100, `cash` carries the spend
   (`100 - spent`), `positions` carries the net base held, `order_attempts` carries the full consumed
   count (9), and `attempt_limit` is left **unset** rather than invented — the limit is a policy
   decision for the run that follows. No directory is rebuilt to reclaim budget or attempts.
5. **The reward anchor is re-based** at the migration mark with a fresh public quote, and the
   discontinuity is recorded (`anchor_rebased`, `anchor_mark`) rather than hidden. Merging historical
   anchors would be averaging in disguise.
6. **Checkpoints are never averaged or spliced.** The newest checkpoint on the verifiable timeline is
   chosen (`checkpoint_chosen`) and every candidate with its position is recorded
   (`checkpoint_candidates`), so the choice is reviewable. On this account the choice is
   `runs/okx-demo-final`'s `brain-1.npz` — the newest directory on the timeline — over `okx-demo`
   (tick 10, earlier), `accept2`, `accept3` and `restart`. The file itself is then **copied, not
   merely referenced**: the source hash is verified against the hash the migration recorded, the copy
   is written beside the target and renamed into place (so a crash cannot leave a half-written
   checkpoint where a run would pick it up), and the target hash is verified afterwards. The source
   directory, file, hash and result are recorded (`checkpoint_source`, `checkpoint_result`). An
   existing target file with *different* content is never overwritten; if it already matches, nothing
   is written. Neural state is never regenerated, re-derived or merged.
7. **History is preserved**: every order (settled or rejected), every adjudication record, every
   retired acknowledgement and every protocol migration is carried into the unified ledger, and the
   source directories are left in place.

### Completing a prepared migration

If a checkpoint file is missing from an already-prepared directory, it can be filled in place without
redoing the migration:

```sh
.venv\Scripts\python.exe tools\okx_ledger_migrate.py --repair-checkpoint
```

This copies the file from the source the migration already recorded (verified on both sides, refusing
a conflicting existing file) and appends an audit entry to `checkpoint_repairs`. Cash, positions,
budget, orders, attempt counters, the reward anchor, the checkpoint *metadata* and `migration.state`
are all left exactly as they were: it completes a prepared migration, it does not redo it. It runs
under the same worker lock as promotion.

### Promotion

Promotion is a separate, recorded decision, and it re-checks rather than trusts:

```sh
# Report whether promotion would be allowed. Changes nothing.
.venv\Scripts\python.exe tools\okx_ledger_migrate.py --check-promotion

# Promote, if and only if every check passes.
.venv\Scripts\python.exe tools\okx_ledger_migrate.py --confirm-promotion
```

Both take the run directory's worker lock first, so no worker can be running against it, and both run
the same evaluation on an identically opened ledger. Promotion requires:

1. `migration.state == "staged"`;
2. **artifact integrity** — the recorded `carried_orders` and `attempts_carried` still match the
   ledger, every recorded gift cross-check still passes, `baseline`/`budget`/`cash`/`positions` are
   present, the budget still equals the configured capital, and no intent is unresolved;
3. **the checkpoint** — metadata present, the file name is a plain `brain-N.npz` that resolves inside
   the run directory (no traversal, no absolute path, nothing else), the file exists, its hash matches
   the ledger, and the **current `FlyController` can actually restore it**. A matching hash proves the
   bytes are unchanged; only attempting the restore proves the state is compatible with the current
   graph, rule and configuration, so it is attempted. Missing, tampered, illegal or incompatible
   state refuses promotion;
4. **live preconditions** — identity, account mode, risk fields, untriggered algo coverage and
   balance reconciliation all pass against the account *right now*, via the same `verify_balances()`
   a run performs before its first tick.

The state flip then happens inside a single transaction that re-reads the state, so a concurrent
change cannot be overwritten, and **any failure leaves the ledger staged**. Flipping one field is
not promotion and is not treated as acceptance.

## Offline pretraining

`tools/fly_pretrain.py` runs the unchanged fly — full connectome, dopamine-modulated plasticity
rule, fixed decoder, reinforcement signal, guard sizing — over a replayed past instead of the live
present, from public candles cached under `runs/pretrain-BTC-USDT/`. One tick consumes one 1-minute
candle; a virtual clock advances 60 virtual seconds per tick, so cooldowns and the daily cap shape
order density exactly as live while the wall clock runs as fast as the CPU allows (~0.22 ticks/s
here). Paper fills charge the demo environment's actual 0.1% taker fee. Runs are resumable (a
paper-mode ledger carries tick, virtual clock, checkpoint and accounting; the candle cache is the
dataset — resume indexes into exactly the array earlier ticks trained on).

Two honest boundaries, enforced rather than implied: nothing here injects a strategy (no labels, no
weight surgery, no external policy — knowledge only grows through the existing plasticity rule), and
no outcome of an offline exploration may be claimed as validated profitable learning — every report
carries that label. Deploying a pretrained checkpoint into a run directory is a separate, recorded
decision (`--deploy-to`, refused while the worker holds the lock, audited in
`checkpoint_deployments` with what was replaced, by what, from where).

## Reinforcement shaping and portfolio-state overlay (experiment, live)

Two engineered levers changed the fly's experience on 2026-09-19 (recorded in
`settings_migrations` + `protocol_migrations`); both keep the reinforcement strictly
equity-derived and the decoder fixed:

- **Trade-anchored reward** (`--reward-anchor trade --reward-horizon-ticks 30`): the
  reward anchor is re-based when a trade settles (and every 30 ticks as a fallback)
  instead of every tick. Each pulse compares current portfolio equity with that
  anchor; this does not isolate the causal consequence of the previous action.
  After selling all holdings, a rising market does not itself lower cash equity
  and therefore does not create an aversive pulse for missed gains. Repeated
  observations can reinforce the same cumulative change, and rebasing after a
  fill excludes its immediate fee loss from the next comparison. These are
  experimental reward semantics, not demonstrated improvements in learning.
- **Portfolio-state overlay** (`--show-portfolio-state`): the observed frame now also
  draws the bot's own equity curve (violet, same window) and a cash-fraction bar
  (bottom edge) — sensory information about its own state the fly otherwise cannot
  access, since its chart shows only the market. Whether the decoder can use this
  is up to exploration, not a guarantee.

Defaults remain the original behavior (`--reward-anchor tick`, no overlay); the
offline pretraining tool takes matching flags so a pretrained brain trains
in-distribution with the run it will join.

## Output hygiene

Nothing printed by the CLI, the diagnostic, or any broker exception contains an account id, key,
secret, passphrase, signature, raw balance, or response body. Broker errors name only a category —
a field name, a currency code, an `ordType` or an OKX business code. `status` reports
`account_bound: true/false` instead of the account id. `provenance.json` records the attempt budget.
Retired `algo_coverage_gap_ack` values are still shown by `status` as
`algo_coverage_gap_ack_retired`, as history only: nothing reads them.

## Boundaries and limitations

- OKX has no quote-increment for spot, so that `Quote` field stays `None`. It DOES enforce a minimum
  order value that the API does not publish: sub-1-USDT orders are rejected with `sCode 51020`
  ("Your order should meet or exceed the minimum order amount"; OKX documents a 1 USDT minimum for
  BTC-USDT). The adapter encodes it as `minimum_quote = 1 USDT`, so the guard vetoes unexecutable
  plans locally — a rejected submission still consumes the lifetime attempt budget,
  though not the daily fill cap. (The instruments payload does publish the price-band coefficients —
  `floatPxLmtPct 0.005` / `maxPxLmtPct 0.01` — which matched the observed 51138 price-limit
  rejections exactly.)
- OKX has no pre-trade fee preview comparable to Coinbase's; fees are known only after fill. The fee
  ceiling is therefore enforced at settlement: an actual fee above the previewed ceiling records the
  fill and halts further orders. The sell-side base-fee reserve is bounded by the configured
  `fee_reserve` (≤ 5%), so a fee beyond that would still be discovered at settlement.
- The demo account is not exclusive to the bot, and never was: it carries the account's own prior
  orders from before this project. The bot treats everything it did not buy as unallocated and never
  sells it, and any third-party trade is detected by reconciliation and stops the worker rather than
  being absorbed.
- Untriggered algo-order coverage depends on OKX answering `orders-algo-pending`; when it does not,
  the run stops instead of proceeding on an unverified account state. That is a deliberate
  availability-dependent stop, not a gap that can be waived.
- The record/replay state a run needs is the checkpoint plus the ledger's accounting. `observation`
  (the market-history window and last neural summary) is deliberately **not** carried by a migration:
  `OKXMarket.snapshot()` repopulates `history` from completed public candles whenever it is empty and
  `market_frame` accepts any window length, so recovery does not depend on it, and copying another
  directory's window would mix timelines into the new run.
- This is an in-program budget, not exchange-level fund isolation. The account must not be shared
  while the bot runs.
- The full retained connectome and fixed decoder are preserved. Optional reward anchoring and
  portfolio rendering change the experiment protocol and are recorded explicitly. The risk guard
  may only reject an order; it never substitutes one.
