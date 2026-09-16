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


class OKXMarket:
    def __init__(self, products, client=None):
        if client is None:
            from .okx_client import OKXClient

            client = OKXClient(api_key=None, secret=None, passphrase=None)
        self.client = client
        self.products = products
        self.history = {p: [] for p in products}

    def _instrument(self, product):
        inst = self.client.instruments(product)
        if inst is None:
            raise RuntimeError("Instrument not found")
        if inst.get("instId") != product or inst.get("instType") != "SPOT":
            raise RuntimeError("Unexpected instrument")
        if (
            inst.get("baseCcy") != product.split("-")[0]
            or inst.get("quoteCcy") != "USDC"
        ):
            raise RuntimeError("Unexpected instrument currencies")
        if inst.get("state") != "live":
            raise RuntimeError("Product unavailable for immediate spot execution")
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
            raise RuntimeError("No completed historical candles available")
        if any(not math.isfinite(v) or v <= 0 for v in closes):
            raise RuntimeError("Invalid historical price")
        return closes

    def snapshot(self):
        result = {}
        for product in self.products:
            if not self.history[product]:
                self.history[product] = self._history(product)
            inst = self._instrument(product)
            tk = self.client.ticker(product)
            if tk is None or tk.get("instId") != product:
                raise RuntimeError("Empty or mismatched ticker")
            bid_raw = tk.get("bidPx")
            ask_raw = tk.get("askPx")
            ts_raw = tk.get("ts")
            if not bid_raw or not ask_raw or not ts_raw:
                raise RuntimeError("Ticker missing best bid/ask or timestamp")
            bid = D(bid_raw)
            ask = D(ask_raw)
            ts = int(ts_raw) / 1000.0
            lot = D(inst["lotSz"])   # base quantity increment
            min_sz = D(inst["minSz"])  # minimum base size
            tick = D(inst["tickSz"])   # price increment
            # OKX spot defines exactly three size/price rules: lotSz (base
            # lot), minSz (minimum base) and tickSz (price tick). It has no
            # quote-amount increment and no quote minimum, so those two Quote
            # fields are None to express "no such rule" — the shared guard
            # then skips the quote-minimum check rather than enforcing a
            # fabricated limit (which would wrongly reject a minimum-size sell
            # after slippage).
            quote = Quote(
                product,
                bid,
                ask,
                ts,
                lot,
                None,
                tick,
                None,
                min_sz,
            )
            result[product] = quote
        return result

    def record(self, quotes):
        for p, q in quotes.items():
            self.history[p].append(float((q.bid + q.ask) / 2))
            self.history[p] = self.history[p][-120:]
