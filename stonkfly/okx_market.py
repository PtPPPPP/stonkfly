"""Public OKX spot observations, shaped to the shared Stonkfly Quote model.

The OKX order-book/ticker timestamp is millisecond Unix epoch; the candle list
is newest-first with a trailing ``confirm`` flag. Only completed, past candles
become price history. ``snapshot()`` never appends history; the run loop calls
``record()`` once per observation so an execution-price refresh is not counted
as another neural observation.
"""

import math
import time

from .config import D
from .market import Quote
from .okx_client import TransientReadError


class OKXMarket:
    # Instrument descriptors (lotSz/tickSz/minSz/state) change rarely; refetch
    # at most this often. A suspension within the TTL window surfaces as an
    # exchange rejection instead -- a definite, non-fatal outcome.
    INSTRUMENT_TTL_SECONDS = 300.0

    def __init__(self, products, client=None):
        if client is None:
            from .okx_client import OKXClient

            client = OKXClient(api_key=None, secret=None, passphrase=None)
        self.client = client
        self.products = products
        self.history = {p: [] for p in products}
        self._instruments = {}

    def _instrument(self, product):
        cached = self._instruments.get(product)
        if cached is not None and time.time() - cached[0] < self.INSTRUMENT_TTL_SECONDS:
            return cached[1]
        inst = self.client.instruments(product)
        if inst is None:
            raise RuntimeError("Instrument not found")
        if inst.get("instId") != product or inst.get("instType") != "SPOT":
            raise RuntimeError("Unexpected instrument")
        base, quote = product.split("-")
        if inst.get("baseCcy") != base or inst.get("quoteCcy") != quote:
            raise RuntimeError("Unexpected instrument currencies")
        if inst.get("state") != "live":
            raise RuntimeError("Product unavailable for immediate spot execution")
        self._instruments[product] = (time.time(), inst)
        return inst

    def _history(self, product):
        candles = self.client.candles(product, bar="1m", limit=120)
        now_ms = int(time.time() * 1000)
        past = [
            c
            for c in candles
            if len(c) >= 9 and c[8] == "1" and int(c[0]) < now_ms
        ]
        past.sort(key=lambda c: int(c[0]))
        closes = [float(c[4]) for c in past]
        if not closes:
            # Unreadable data, not a definitive answer: the run loop may skip
            # the tick and retry, instead of halting the whole run.
            raise TransientReadError("No completed historical candles available")
        if any(not math.isfinite(v) or v <= 0 for v in closes):
            raise TransientReadError("Invalid historical price")
        return closes

    def snapshot(self):
        result = {}
        for product in self.products:
            if not self.history[product]:
                self.history[product] = self._history(product)
            inst = self._instrument(product)
            tk = self.client.ticker(product)
            if tk is None or tk.get("instId") != product:
                raise TransientReadError("Empty or mismatched ticker")
            bid_raw = tk.get("bidPx")
            ask_raw = tk.get("askPx")
            ts_raw = tk.get("ts")
            if not bid_raw or not ask_raw or not ts_raw:
                raise TransientReadError("Ticker missing best bid/ask or timestamp")
            bid = D(bid_raw)
            ask = D(ask_raw)
            ts = int(ts_raw) / 1000.0
            lot = D(inst["lotSz"])   # base quantity increment
            min_sz = D(inst["minSz"])  # minimum base size
            tick = D(inst["tickSz"])   # price increment
            # OKX spot defines exactly three size/price rules: lotSz (base
            # lot), minSz (minimum base) and tickSz (price tick), and has no
            # quote-amount increment. It DOES enforce a minimum order value
            # that the instruments payload does not publish: buys of ~0.94
            # USDT notional were rejected with sCode 51020 ("Your order should
            # meet or exceed the minimum order amount"), while OKX documents a
            # 1 USDT minimum for BTC-USDT. Encoding it here makes the guard
            # veto unexecutable plans locally instead of burning an attempt
            # and the daily quota on an order the exchange will refuse.
            quote = Quote(
                product,
                bid,
                ask,
                ts,
                lot,
                None,
                tick,
                D("1"),
                min_sz,
                D(inst["floatPxLmtPct"]) if inst.get("floatPxLmtPct") else None,
            )
            result[product] = quote
        return result

    def record(self, quotes):
        for p, q in quotes.items():
            self.history[p].append(float((q.bid + q.ask) / 2))
            self.history[p] = self.history[p][-120:]
