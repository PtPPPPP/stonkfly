"""Minimal OKX v5 REST client: HMAC signing, no automatic order retries.

Public market-data endpoints are called without credentials. Private endpoints
(demo trading) require an API key/secret/passphrase and, for the demo
environment, the ``x-simulated-trading: 1`` header. Order placement is a single
fire-and-forget HTTP POST with no retry: an ambiguous transport outcome must be
reconciled, never resubmitted.

Reference: https://www.okx.com/docs-v5/en/
"""

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Product identity sent on every request. OKX's CDN blocks urllib's default
# "Python-urllib/3.x" user-agent; we send our own real product string without
# impersonating curl or a browser, and without rotating it.
USER_AGENT = "stonkfly/0.1.0"


class OKXTransportError(Exception):
    """No well-formed HTTP 200 response was received from OKX.

    ``status`` carries the HTTP status code when the server returned a non-200
    response (e.g. 502/504/429); it is ``None`` for connection-level failures
    (timeout, DNS, connection reset) where no response was received at all. The
    chained ``__cause__`` preserves the underlying connection exception.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class OKXBusinessError(Exception):
    """OKX returned a non-zero business code where data was required."""


class OKXClient:
    # Default global domain; region-specific accounts use us.okx.com / eea.okx.com.
    BASE = "https://www.okx.com"

    def __init__(
        self,
        api_key=None,
        secret=None,
        passphrase=None,
        demo=False,
        timeout=10,
        base=None,
        transport=None,
        clock=None,
        opener=None,
    ):
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase
        self.demo = demo
        self.timeout = timeout
        self.base = base or self.BASE
        self._transport = transport or self._http
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._opener = opener

    # -- signing -----------------------------------------------------------
    def _timestamp(self):
        return self._clock().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _sign(self, timestamp, method, path, body):
        message = f"{timestamp}{method}{path}{body}"
        digest = hmac.new(
            self.secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        )
        return base64.b64encode(digest.digest()).decode("utf-8")

    def request(self, method, path, params=None, body=None, auth=False):
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params, doseq=True)
        url = self.base + path + query
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        payload_bytes = None
        body_str = ""
        if body is not None:
            body_str = json.dumps(body, separators=(",", ":"))
            payload_bytes = body_str.encode("utf-8")
        if auth:
            ts = self._timestamp()
            headers["OK-ACCESS-KEY"] = self.api_key
            headers["OK-ACCESS-SIGN"] = self._sign(ts, method, path + query, body_str)
            headers["OK-ACCESS-TIMESTAMP"] = ts
            headers["OK-ACCESS-PASSPHRASE"] = self.passphrase
            if self.demo:
                headers["x-simulated-trading"] = "1"
        try:
            status, data = self._transport(method, url, headers, payload_bytes)
        except OKXTransportError:
            raise
        except Exception as e:
            # Any failure to obtain a well-formed HTTP 200 response is an
            # ambiguous transport outcome (timeout, connection loss, malformed
            # body), never a definite order rejection.
            raise OKXTransportError("OKX transport error") from e
        if status != 200:
            raise OKXTransportError("OKX HTTP error", status=status)
        return data

    def _http(self, method, url, headers, data):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        open_url = self._opener.open if self._opener is not None else urllib.request.urlopen
        try:
            with open_url(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as e:
            # Non-200 (4xx/5xx/429) is an ambiguous infrastructure response.
            # Preserve the status code and the underlying HTTPError for diagnostics.
            raise OKXTransportError("OKX HTTP error", status=e.code) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OKXTransportError("OKX transport error") from e

    def _require_ok(self, d, what):
        """Validate the OKX envelope and return the decoded body.

        A query that fails (non-zero ``code``) or returns a malformed body must
        raise, never be interpreted as an empty result set. ``what`` names the
        endpoint for the error message.
        """
        if not isinstance(d, dict):
            raise OKXBusinessError(f"{what}: malformed OKX response")
        code = d.get("code")
        if code != "0":
            raise OKXBusinessError(f"{what}: OKX code={code} msg={d.get('msg')}")
        return d

    # -- public endpoints --------------------------------------------------
    def public_time(self):
        d = self._require_ok(self.request("GET", "/api/v5/public/time"), "public time")
        data = d.get("data") or []
        return int((data[0] if data else {}).get("ts", "0"))

    def instruments(self, inst_id):
        d = self._require_ok(
            self.request(
                "GET",
                "/api/v5/public/instruments",
                params={"instType": "SPOT", "instId": inst_id},
            ),
            "instruments",
        )
        data = d.get("data") or []
        return data[0] if data else None

    def ticker(self, inst_id):
        d = self._require_ok(
            self.request("GET", "/api/v5/market/ticker", params={"instId": inst_id}),
            "ticker",
        )
        data = d.get("data") or []
        return data[0] if data else None

    def candles(self, inst_id, bar="1m", limit=120):
        d = self._require_ok(
            self.request(
                "GET",
                "/api/v5/market/candles",
                params={"instId": inst_id, "bar": bar, "limit": str(limit)},
            ),
            "candles",
        )
        data = d.get("data")
        return data if isinstance(data, list) else []

    # -- private endpoints (demo trading) ----------------------------------
    def account_config(self):
        d = self._require_ok(
            self.request("GET", "/api/v5/account/config", auth=True), "account config"
        )
        data = d.get("data") or []
        return data[0] if data else None

    def balance(self, ccys):
        d = self._require_ok(
            self.request(
                "GET",
                "/api/v5/account/balance",
                params={"ccy": ",".join(ccys)},
                auth=True,
            ),
            "balance",
        )
        return d.get("data") or []

    def place_order(self, payload):
        # Single submission. No retry: an ambiguous outcome is reconciled later.
        return self.request("POST", "/api/v5/trade/order", body=payload, auth=True)

    def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
        params = {"instId": inst_id}
        if ord_id:
            params["ordId"] = ord_id
        elif cl_ord_id:
            params["clOrdId"] = cl_ord_id
        d = self.request("GET", "/api/v5/trade/order", params=params, auth=True)
        if not isinstance(d, dict):
            raise OKXBusinessError("order query: malformed OKX response")
        code = d.get("code")
        if code == "51603":  # "Order does not exist" — a clean not-found
            return None
        if code != "0":
            raise OKXBusinessError(f"order query: OKX code={code} msg={d.get('msg')}")
        data = d.get("data") or []
        return data[0] if data else None

    def orders_pending(self, inst_id):
        d = self._require_ok(
            self.request(
                "GET",
                "/api/v5/trade/orders-pending",
                params={"instId": inst_id, "instType": "SPOT"},
                auth=True,
            ),
            "orders pending",
        )
        data = d.get("data")
        if not isinstance(data, list):
            raise OKXBusinessError("orders pending: malformed data")
        return data
