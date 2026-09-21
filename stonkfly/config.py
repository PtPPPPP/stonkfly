import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal


def D(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not money")
    x = Decimal(str(value))
    if not x.is_finite():
        raise ValueError("Nonfinite quantity")
    return x


def down(value, step):
    return (D(value) / D(step)).to_integral_value(rounding=ROUND_DOWN) * D(step)


def up(value, step):
    return (D(value) / D(step)).to_integral_value(rounding=ROUND_UP) * D(step)


# Spot pairs the system is allowed to run. OKX demo trades against USDT quotes
# while Coinbase (and the default paper feed) trade against USDC; both quote
# families are allowlisted so the mode chooses its own, and a run must never mix
# quotes.
ALLOWED_PRODUCTS = (
    "BTC-USDC", "ETH-USDC", "SOL-USDC",
    "BTC-USDT", "ETH-USDT", "SOL-USDT",
)


@dataclass(frozen=True)
class Settings:
    products: tuple[str, ...] = ("BTC-USDC",)
    capital: str = "100"
    order_limit: str = "10"
    loss_stop: str = "20"
    fee_reserve: str = "0.02"
    slippage: str = "0.005"
    spread_limit: str = "0.005"
    daily_orders: int = 24
    interval_seconds: float = 60
    max_quote_age: float = 15
    neural_ms: float = 500
    neural_bin_ms: float = 10
    pulse_ms: float = 200
    pulse_current: float = 20
    reward_deadband: str = "0.01"
    decoder_threshold_hz: float = 2
    paper_fee: str = "0.006"
    learning: bool = True
    # Reinforcement shaping. "tick" re-anchors the reward every tick (each pulse
    # is the one-tick equity change); "trade" re-anchors only at settled trades
    # (and every reward_horizon_ticks as a fallback), so each pulse grades the
    # consequence of the action before it -- a sell followed by a rising market
    # lands as aversive instead of being masked by per-tick drift.
    reward_anchor: str = "tick"
    reward_horizon_ticks: int = 1
    # Sensory encoding: when True, the rendered frame also draws the bot's own
    # equity curve over the same window and a cash-fraction bar, giving the
    # decoder input access to the portfolio state it otherwise cannot see.
    show_portfolio_state: bool = False
    # Availability policy for READ-ONLY data (market snapshot, balance/risk
    # sweeps). A transient read failure never trades on stale data and never
    # touches an intent: the tick is skipped and recorded. Only when this many
    # consecutive ticks have been skipped does the run escalate to a halt, so a
    # short proxy/network blip degrades instead of stopping the worker. This
    # never applies to order submission, reconciliation or ledger writes: those
    # halt immediately exactly as before.
    read_fail_halt_after: int = 5
    # Maximum tolerated |exchange_time - local_time| in seconds, measured at
    # preflight against OKX public time. Beyond this the quote-freshness window
    # ([-0.5 s, max_quote_age]) cannot be trusted, so preflight refuses instead
    # of starting. Kept well inside OKX's ±30 s signing tolerance.
    clock_offset_max: float = 5.0
    # Per-tick budget for the READ-ONLY observation phase (balance/risk sweep,
    # market snapshot, clock refresh). When the budget is spent the tick is
    # skipped like any other transient read failure -- it never cuts off an
    # order submission: the send boundary and everything after it (settle
    # polling, reconciliation) are exempt by construction.
    tick_budget_seconds: float = 45.0
    # events.jsonl rotation: rotate when the file exceeds this many MB, keep
    # this many rotated generations (events.1.jsonl .. events.N.jsonl).
    events_rotate_mb: int = 32
    events_keep: int = 3
    # Minimum completed candles before the decoder may act on a rendered
    # frame: market_frame draws history[-100:], so fewer candles render a
    # chart the fly sees only partially. In warmup the tick observes nothing,
    # submits nothing and is recorded, never vetoed or halted. The upper
    # bound matches the market modules' 120-candle history cap: a warmup
    # above it could never complete.
    warmup_candles: int = 100

    @property
    def quote_ccy(self):
        """The single quote currency shared by every configured product."""
        return self.products[0].split("-")[1]

    def __post_init__(self):
        if (
            not self.products
            or len(set(self.products)) != len(self.products)
            or not set(self.products) <= set(ALLOWED_PRODUCTS)
        ):
            raise ValueError("Only allowlisted USDC/USDT spot pairs")
        if len({p.split("-")[1] for p in self.products}) != 1:
            raise ValueError("All products must share one quote currency")
        if not 0 < D(self.capital) <= 100 or not 0 < D(self.order_limit) <= min(
            D(self.capital), D(10)
        ):
            raise ValueError("Maximum capital $100; maximum order $10")
        if not 0 < D(self.loss_stop) <= D(self.capital):
            raise ValueError("Invalid loss stop")
        if (
            not D(0) < D(self.fee_reserve) <= D(".05")
            or not 0 <= D(self.slippage) <= D(".01")
            or not 0 < D(self.spread_limit) <= D(".01")
        ):
            raise ValueError("Invalid fee/spread/slippage bounds")
        if not 0 <= D(self.paper_fee) <= D(self.fee_reserve):
            raise ValueError("Invalid paper fee")
        if (
            type(self.daily_orders) is not int
            or not 1 <= self.daily_orders <= 100
            or not math.isfinite(self.interval_seconds)
            or self.interval_seconds < 60
        ):
            raise ValueError("Rate limit: >=60 s between orders, <=100 orders/day")
        if D(self.reward_deadband) <= 0:
            raise ValueError("Positive reinforcement deadband required")
        if self.reward_anchor not in ("tick", "trade"):
            raise ValueError("reward_anchor must be 'tick' or 'trade'")
        if (
            type(self.reward_horizon_ticks) is not int
            or self.reward_horizon_ticks < 1
            or type(self.show_portfolio_state) is not bool
        ):
            raise ValueError("Invalid reward horizon or portfolio-state flag")
        if (
            type(self.read_fail_halt_after) is not int
            or not 1 <= self.read_fail_halt_after <= 60
        ):
            raise ValueError("read_fail_halt_after must be an integer in 1..60")
        if not math.isfinite(self.clock_offset_max) or not 1 <= self.clock_offset_max <= 30:
            raise ValueError("clock_offset_max must be 1..30 seconds")
        if not math.isfinite(self.tick_budget_seconds) or not 15 <= self.tick_budget_seconds <= 600:
            raise ValueError("tick_budget_seconds must be 15..600")
        if (
            type(self.events_rotate_mb) is not int
            or not 1 <= self.events_rotate_mb <= 1024
            or type(self.events_keep) is not int
            or not 1 <= self.events_keep <= 20
        ):
            raise ValueError("Invalid events rotation bounds")
        if type(self.warmup_candles) is not int or not 1 <= self.warmup_candles <= 120:
            raise ValueError("warmup_candles must be an integer in 1..120")
        for x in [
            self.max_quote_age,
            self.neural_ms,
            self.neural_bin_ms,
            self.pulse_ms,
            self.pulse_current,
            self.decoder_threshold_hz,
        ]:
            if not math.isfinite(x) or x <= 0:
                raise ValueError("Positive finite parameter required")
        if self.neural_bin_ms > 10 or self.pulse_ms > self.neural_ms:
            raise ValueError(
                "Use <=10 ms neural bins; pulse must fit a decision window"
            )
        if any(
            abs(x * 10 - round(x * 10)) > 1e-7
            for x in [self.neural_ms, self.neural_bin_ms, self.pulse_ms]
        ):
            raise ValueError("Neural intervals must be multiples of 0.1 ms")

    def signature(self):
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()
