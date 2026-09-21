"""Execution limits can veto a neural proposal, never substitute a strategy."""

import time

from .config import D, down, up


class Veto(Exception):
    pass


class Guard:
    def __init__(self, settings, ledger, stop_file, clock=time.time):
        # ``clock`` is the validated time basis: cli passes a closure adjusted
        # by the preflight-measured OKX clock offset, so quote freshness and
        # cooldowns are judged against exchange time, not a possibly drifting
        # local clock. Default is the wall clock (paper/fixture runs).
        self.s = settings
        self.l = ledger
        self.stop_file = stop_file
        self.clock = clock

    def check(self, quotes, now):
        if self.stop_file.exists():
            raise Veto("STOP file present")
        if self.l.get("halted"):
            raise Veto(self.l.get("halted"))
        if self.l.pending():
            raise Veto("Order outcome unresolved")
        if set(quotes) != set(self.s.products):
            raise Veto("Incomplete market snapshot")
        for product, q in quotes.items():
            if q.product != product:
                raise Veto("Quote identity mismatch")
            if not -0.5 <= now - q.timestamp <= self.s.max_quote_age:
                raise Veto("Stale or future quote")
            if (q.ask - q.bid) / q.bid > D(self.s.spread_limit):
                raise Veto("Spread limit")
        if self.l.equity(quotes) <= D(self.l.get("initial_cash")) - D(self.s.loss_stop):
            self.l.halt("Loss stop reached; holdings remain exposed")
            raise Veto("Loss stop reached")

    def plan(self, product, side, quotes, now=None):
        now = self.clock() if now is None else now
        self.check(quotes, now)
        if product not in self.s.products or side not in ("BUY", "SELL"):
            raise Veto("Invalid neural proposal")
        if now - self.l.get("last_attempt") < self.s.interval_seconds:
            raise Veto("Order cooldown")
        if self.l.filled_today(now) >= self.s.daily_orders:
            # Fill-based daily cap: it bounds real trading, so rejected or
            # zero-filled submissions do not consume it. Runaway submissions
            # stay bounded by the cooldown, the worker halt and the lifetime
            # attempt budget (which remains submission-based).
            raise Veto("Daily order limit")
        remaining = self.l.attempts_remaining()
        if remaining == 0:  # None means the run directory is unbounded
            raise Veto("Order attempt budget exhausted")
        q = quotes[product]
        reserve = D(self.s.fee_reserve)
        if side == "BUY":
            limit = up(q.ask * (1 + D(self.s.slippage)), q.price_increment)
            budget = min(D(self.s.order_limit), self.l.cash) / (1 + reserve)
            size = down(budget / limit, q.base_increment)
        else:
            # OKX rejects a sell priced below its dynamic price band (observed as
            # sCode 51138, "The lowest price limit for sell orders is {param0}"):
            # the band sits at about -0.5% from last, and because bid <= last and
            # the price is rounded down to the tick, a buffer equal to the band
            # lands on or under the line by construction -- 7 of 7 sells priced at
            # bid*(1-0.005) were rejected, while every buy at +0.5% passed because
            # rounding up keeps it on the safe side of the mirror code (51137).
            # So the sell buffer is capped well inside the band, and the order
            # carries pxAmendType=1 so anything still outside the undisclosed,
            # dynamic band is amended to its edge rather than rejected.
            # The band's floating coefficient is published per instrument
            # (floatPxLmtPct, 0.005 for BTC-USDT); when available the buffer
            # derives from it (40% of the band), adapting if OKX retunes the
            # band, and falls back to the observed value otherwise.
            # Economics are unchanged: a FOK sell fills at the bid of the moment
            # or cancels unharmed, the limit is only the floor the exchange
            # will accept.
            band = q.float_px_lmt_pct if q.float_px_lmt_pct is not None else D("0.005")
            sell_buffer = min(D(self.s.slippage), band * D("0.4"))
            limit = down(
                q.bid * (1 - sell_buffer),
                q.price_increment,
            )
            # A SELL may only consume the bot's own inventory: the exchange sees
            # the gifted base too, so anything above ``held`` would sell assets
            # the bot does not own. The reserve keeps room for a base-currency
            # fee, which is deducted from the position after the fill and would
            # otherwise drive it negative only once the trade was done.
            held = self.l.positions.get(product, D(0))
            size = down(
                min(held / (1 + reserve), D(self.s.order_limit) / q.ask),
                q.base_increment,
            )
        if (
            limit <= 0
            or size < q.minimum_base
            or (q.minimum_quote is not None and size * limit < q.minimum_quote)
        ):
            raise Veto("Insufficient funds/position or below exchange minimum")
        return {
            "product": product,
            "side": side,
            "base_size": str(size),
            "limit_price": str(limit),
            "fee_ceiling": str(size * limit * reserve),
            "observed_bid": str(q.bid),
            "observed_ask": str(q.ask),
            "quote_timestamp": q.timestamp,
            "settings": self.s.signature(),
            "order_type": "limit_limit_fok",
        }

    def before_submit(self, plan):
        # Called after exchange preview and balance checks, at the final send boundary.
        if self.stop_file.exists() or self.l.get("halted"):
            raise Veto("Execution stopped")
        if not -0.5 <= self.clock() - plan["quote_timestamp"] <= self.s.max_quote_age:
            raise Veto("Quote expired before submission")
        pending = self.l.pending()
        if (
            len(pending) != 1
            or pending[0]["id"] != plan["client_order_id"]
            or pending[0]["status"] != "PREPARED"
        ):
            raise Veto("Intent ownership mismatch")
