# Watching the fly's decisions

Two read-only views of what the fly is doing during a run: a terminal panel
(`watch`) and a LAN web dashboard (`serve`). Both render the observation →
reinforcement → neural propagation → fixed decoder → execution pipeline for
every tick, using only files the run loop already writes. Neither holds a
worker lock nor writes anything to the run directory, so observing cannot
influence execution.

## Watch in the browser (LAN)

```sh
python -m stonkfly serve --out runs/paper
```

This prints the local and LAN URLs (for example `http://192.168.1.5:8400`) and
serves a Chinese-language dashboard that any browser on the network can open.
It polls `/api/state` once a second. The page shows a fly whose eyes take the
colour of the latest price move, chest the decision and belly the equity
trend, a multi-row chart of the exact sensory input, the sensory → brain →
action signal path, the decoder's DNp20 left/right rates and DNpe017 gate, the
decision with its execution outcome, and the recent-behaviour histogram.
`--host`/`--port` override the binding (default `0.0.0.0:8400`).

The API is GET-only and exposes run metadata, events and prices — never
account identifiers, cash, positions or credentials.

## Watch in the terminal

## Follow a live run

Start a run in one terminal:

```sh
python -m stonkfly run --fixture --out runs/demo
```

Then watch it in another:

```sh
python -m stonkfly watch --out runs/demo
```

The view starts from the most recent recorded tick (`latest.json`) and
follows `events.jsonl` as the run appends to it. Run metadata (halted state,
mode) is read from `ledger.sqlite` with a read-only connection.

## Replay a recorded run

```sh
python -m stonkfly watch --out runs/paper --replay            # last 300 ticks
python -m stonkfly watch --out runs/paper --replay --all      # from the first tick
python -m stonkfly watch --out runs/paper --replay --speed 10 # faster playback
```

## Reading the terminal panel

The frame is drawn top to bottom in the order the fly's pipeline actually runs:

- **Fly + market chart** — a top-view fly whose body is the run's state at a
  glance: the compound eyes take the colour of the latest price move (green
  up, red down), the chest takes the colour of the current decision, and the
  belly tracks the equity trend. Beside it, the multi-row price area chart is
  the exact sensory input the fly receives.
- **SENSORY → BRAIN → ACTION** — the signal path as one line: eyes → lamina →
  Kenyon cells (spike count) → MBON (mean memory efficacy) → DNp20 → decision.
  Below it, the dopamine pulse applied this tick (PAM11 reward or PPL101
  aversive, with the P&L delta that triggered it), the DNp20 left/right rates
  on a SELL↔BUY balance whose marker sits at the decoded side, the DNpe017
  gate (closed gate forces HOLD), and what execution did (SETTLED, VETO, HOLD).
- **RECENT BEHAVIOUR** — a per-tick histogram where each bar's height is
  |Δ R−L| (how far the decoder input swung) coloured by decision, with the
  decision and stimulus letters underneath, so the fly's behaviour pattern is
  visible at a glance.
- **Memory line** — candidate KC→MBON plasticity state: changed plastic edges
  and mean efficacy relative to baseline. The rule is unvalidated.

## Boundaries

- The watcher is display-only. It cannot place, alter, or retry orders, and a
  crash in it never touches run state.
- The decision is a fixed engineered decoder over neural activity, and the
  reinforcement is an engineered P&L pulse. The panel is not evidence of
  learning or of profitable behaviour.
