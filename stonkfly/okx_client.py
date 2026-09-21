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
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Product identity sent on every request. OKX's CDN blocks urllib's default
# "Python-urllib/3.x" user-agent; we send our own real product string without
# impersonating curl or a browser, and without rotating it.
USER_AGENT = "stonkfly/0.1.0"

# OKX business codes that describe a temporary condition (gateway timeout /
# system busy) rather than a definitive answer. Read-only queries retry these a
# bounded number of times; order placement never does. 51054 is documented as
# "Request timed out. Please try again." (HTTP 500) -- it is NOT a statement
# that a feature is unsupported, so an exhausted retry must never be read as a
# negative answer. See docs/okx.md for the evidence.
_TRANSIENT_CODES = frozenset(("51054", "50004", "50013", "50026"))

# HTTP statuses that describe a temporary condition of the network/gateway
# rather than a definitive answer to a read-only query. Only these (and
# connection-level failures, where no response arrived at all) are retried for
# GETs. Everything else -- 401/403 auth, 404 route, 4xx semantics -- fails
# immediately and is classified as a halt-worthy condition, not a transient.
_RETRYABLE_STATUSES = frozenset((429, 500, 502, 503, 504))


class TransientReadError(RuntimeError):
    """A read-only data source kept failing after its bounded retries.

    Raised only for data the run merely observes (market snapshots, instrument
    descriptors), never for anything that could have changed exchange state.
    The run loop may skip the current tick for this and retry on the next one;
    it must never trade on the stale data that preceded it, and reconciliation,
    order queries and submissions never raise it.

    ``deadline`` marks the budget-exhausted case (per-tick read deadline spent
    before the request could start), as distinct from an exhausted retry.
    """

    def __init__(self, message, code=None, deadline=False):
        super().__init__(message)
        self.code = code
        self.deadline = deadline


def is_transient_read_failure(exc):
    """True when ``exc`` is a recoverable failure of a read-only request.

    This is the whitelist the run loop uses to decide "skip the tick" versus
    "halt": transport loss, a retryable HTTP status, one of OKX's documented
    temporary business codes, or an unreadable market-data payload. Every other
    exception -- program bugs, account-state errors (OKXRiskError), definitive
    business answers, uncertain order outcomes -- must propagate to the halt
    path unchanged.
    """
    if isinstance(exc, TransientReadError):
        return True
    if isinstance(exc, OKXTransportError):
        return exc.status is None or exc.status in _RETRYABLE_STATUSES
    if isinstance(exc, OKXBusinessError):
        return exc.code in _TRANSIENT_CODES
    return False


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
    """OKX returned a non-zero business code where data was required.

    ``code`` carries the OKX business code (or the last transient code when
    bounded retries were exhausted) so a caller can classify the failure
    without parsing the message. The message intentionally carries no balance,
    identifier or request body.
    """

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def demo_client_from_env(env=None, **kwargs):
    """Build a demo-trading client from OKX_API_KEY/SECRET/PASSPHRASE.

    Shared by the broker and the read-only tooling so every entry point signs
    requests and selects the demo environment the same way. Live execution is
    not supported, so this only ever returns a demo client.
    """
    source = os.environ if env is None else env
    key = source.get("OKX_API_KEY")
    secret = source.get("OKX_API_SECRET")
    passphrase = source.get("OKX_API_PASSPHRASE")
    if not (key and secret and passphrase):
        raise RuntimeError(
            "Set OKX_API_KEY, OKX_API_SECRET and OKX_API_PASSPHRASE for OKX demo trading"
        )
    return OKXClient(
        api_key=key, secret=secret, passphrase=passphrase, demo=True, **kwargs
    )


def _descending(ids):
    """True when ids are strictly descending, the documented paging order."""
    for a, b in zip(ids, ids[1:]):
        try:
            if int(a) <= int(b):
                return False
        except (TypeError, ValueError):
            if str(a) <= str(b):
                return False
    return True


class KeepAliveTransport:
    """Persistent HTTP(S) transport with proxy support.

    Read-only requests (GET) reuse one tunnelled connection: each fresh
    connection costs a TLS handshake plus proxy CONNECT (~1.1s here) while a
    reused one costs ~0.55s, and a tick performs roughly a dozen requests. A
    pooled GET that fails on a stale connection is re-established and retried
    once -- reads are idempotent, so the retry cannot change any outcome.
    Requests that can change state (POST) take a fresh connection with a single
    attempt and are never retried: a response lost mid-flight must stay an
    ambiguous outcome for the broker, exactly as with the per-request transport.
    """

    def __init__(self, timeout, proxy=None, connection_factory=None):
        self.timeout = timeout
        proxies = urllib.request.getproxies() if proxy is None else {"https": proxy}
        self.proxy = proxies.get("https") or proxies.get("http")
        self._factory = connection_factory or self._connect
        self._conn = None
        self._lock = threading.Lock()

    def _target(self, url):
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return host, port, parts.path + (("?" + parts.query) if parts.query else "")

    def _connect(self, url):
        host, port, _path = self._target(url)
        if self.proxy:
            u = urllib.parse.urlsplit(
                self.proxy if "://" in self.proxy else "http://" + self.proxy
            )
            conn = http.client.HTTPSConnection(
                u.hostname, u.port or 80, timeout=self.timeout
            )
            tunnel_headers = {}
            if u.username is not None:
                token = base64.b64encode(
                    f"{u.username}:{u.password or ''}".encode()
                ).decode()
                tunnel_headers["Proxy-Authorization"] = f"Basic {token}"
            conn.set_tunnel(host, port, tunnel_headers)
            return conn
        return http.client.HTTPSConnection(host, port, timeout=self.timeout)

    def _round_trip(self, conn, method, url, headers, payload):
        host, port, path = self._target(url)
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, None

    def __call__(self, method, url, headers, payload):
        with self._lock:
            if method == "GET":
                return self._pooled_get(url, headers, payload)
            return self._single_shot(method, url, headers, payload)

    def _pooled_get(self, url, headers, payload):
        for attempt in (0, 1):
            conn = self._conn
            if conn is None:
                conn = self._conn = self._factory(url)
            try:
                return self._round_trip(conn, "GET", url, headers, payload)
            except (http.client.HTTPException, OSError):
                # The pooled connection went stale. Reads are idempotent, so
                # re-establishing and retrying once cannot change any outcome.
                try:
                    self._conn.close()
                except OSError:
                    pass
                self._conn = None
                if attempt:
                    raise
        raise AssertionError("unreachable")

    def _single_shot(self, method, url, headers, payload):
        conn = self._factory(url)
        try:
            return self._round_trip(conn, method, url, headers, payload)
        finally:
            try:
                conn.close()
            except OSError:
                pass


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
        proxy=None,
    ):
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase
        self.demo = demo
        self.timeout = timeout
        self.base = base or self.BASE
        self._transport = transport if transport is not None else KeepAliveTransport(timeout, proxy=proxy)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Read-phase budget hook: when set (a time.monotonic() deadline), every
        # read-only GET through ``_get`` refuses to start past it. Order
        # submission (``place_order``), order queries (``get_order``) and any
        # post-send path never consult it -- a submission in flight is never
        # abandoned and stays single-shot.
        self.read_deadline = None
        # Diagnostics: how many bounded read retries this client has spent
        # since the counter was last cleared by the caller.
        self.read_retries_used = 0

    def _check_read_deadline(self):
        deadline = self.read_deadline
        if deadline is not None and time.monotonic() >= deadline:
            raise TransientReadError(
                "Tick read budget exhausted before this request", deadline=True
            )

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

    def _get(self, path, params=None, auth=False, what="query", retries=3):
        """GET a read-only endpoint with one bounded, shared attempt budget.

        The budget covers both failure classes a read can hit: transport
        failures (timeout, connection reset, dead proxy; status None) and
        retryable HTTP statuses (429/5xx), plus OKX's transient timeout/busy
        business codes (51054/50004/50013/50026). Reads are idempotent, so a
        retry cannot change any outcome, and backoff is short and bounded
        (0.5 s, 1.0 s, ...). Order placement never goes through this path, so
        an ambiguous order outcome is never auto-retried.

        Exhausting the budget raises instead of returning an empty result: a
        failed query must never be mistaken for "nothing there". A definitive
        business code raises OKXBusinessError (a real answer, not a transient);
        an exhausted transient condition raises OKXBusinessError/OKXTransportError
        as before -- the run loop classifies these via
        ``is_transient_read_failure`` and may skip the tick.
        """
        last_code = None
        for attempt in range(retries):
            self._check_read_deadline()
            try:
                d = self.request("GET", path, params=params, auth=auth)
            except OKXTransportError as e:
                if (
                    (e.status is None or e.status in _RETRYABLE_STATUSES)
                    and attempt < retries - 1
                ):
                    self.read_retries_used += 1
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
            if not isinstance(d, dict):
                raise OKXBusinessError(f"{what}: malformed OKX response")
            code = d.get("code")
            if code == "0":
                return d
            last_code = code
            if code in _TRANSIENT_CODES and attempt < retries - 1:
                self.read_retries_used += 1
                time.sleep(0.5 * (attempt + 1))
                continue
            raise OKXBusinessError(f"{what}: OKX code={code}", code=code)
        raise OKXBusinessError(f"{what}: retries exhausted", code=last_code)

    # -- public endpoints --------------------------------------------------
    def public_time(self):
        d = self._get("/api/v5/public/time", what="public time")
        data = d.get("data") or []
        return int((data[0] if data else {}).get("ts", "0"))

    def measure_time_offset(self, samples=5, local_clock=None, mono=None):
        """Estimate ``(exchange_time - local_time)`` in seconds, read-only.

        Takes ``samples`` timestamped round trips of /public/time, keeps the
        lowest-RTT half (the samples least distorted by network jitter) and
        returns the median offset of those. The midpoint of the round trip is
        used as the exchange-clock instant, so a symmetric link needs no
        further correction. No system clock is ever modified; the offset is
        applied at the application layer only. The single-sample failure mode
        (one jittered request) cannot decide anything: several samples are
        required by construction. Returns a dict with ``offset``, ``rtt``
        (median RTT of the kept samples) and ``samples`` ([(rtt, offset), ...]).
        """
        local_clock = local_clock or time.time
        mono = mono or time.monotonic
        if not isinstance(samples, int) or samples < 1:
            raise ValueError("samples must be a positive integer")
        observed = []
        for _ in range(samples):
            t0 = mono()
            ts_ms = self.public_time()
            t1 = mono()
            rtt = t1 - t0
            observed.append((rtt, ts_ms / 1000.0 + rtt / 2.0 - local_clock()))
        observed.sort(key=lambda pair: pair[0])
        kept = observed[: max(1, len(observed) // 2)]
        offsets = sorted(offset for _rtt, offset in kept)
        rtts = sorted(rtt for rtt, _offset in kept)
        return {
            "offset": offsets[len(offsets) // 2],
            "rtt": rtts[len(rtts) // 2],
            "samples": observed,
        }

    def instruments(self, inst_id):
        d = self._get(
            "/api/v5/public/instruments",
            params={"instType": "SPOT", "instId": inst_id},
            what="instruments",
        )
        data = d.get("data") or []
        return data[0] if data else None

    def ticker(self, inst_id):
        d = self._get(
            "/api/v5/market/ticker", params={"instId": inst_id}, what="ticker"
        )
        data = d.get("data") or []
        return data[0] if data else None

    def candles(self, inst_id, bar="1m", limit=120):
        d = self._get(
            "/api/v5/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": str(limit)},
            what="candles",
        )
        data = d.get("data")
        return data if isinstance(data, list) else []

    # -- private endpoints (demo trading) ----------------------------------
    def account_config(self):
        d = self._get("/api/v5/account/config", auth=True, what="account config")
        data = d.get("data") or []
        return data[0] if data else None

    def balance(self, ccys=None):
        # With no ``ccy`` OKX returns every currency that has a balance, which is
        # the full-coverage view required to detect assets the bot is not
        # supposed to touch. ``data`` is a list with one item whose ``details``
        # holds the per-currency rows.
        params = {"ccy": ",".join(ccys)} if ccys else None
        d = self._get("/api/v5/account/balance", params=params, auth=True, what="balance")
        return d.get("data") or []

    def positions(self, inst_type=None):
        # Open derivative/margin positions across all instrument types when
        # ``inst_type`` is omitted. A non-empty list means the account is not a
        # cash-only spot account. Failures raise rather than returning [].
        params = {"instType": inst_type} if inst_type else None
        d = self._get("/api/v5/account/positions", params=params, auth=True, what="positions")
        data = d.get("data")
        if not isinstance(data, list):
            raise OKXBusinessError("positions: malformed data")
        return data

    def account_position_risk(self, inst_type):
        # Snapshot of account and position risk. This is the risk view that
        # exists for spot/futures-mode accounts; /account/risk-state is
        # documented as Portfolio-margin-only. ``instType`` must be one of
        # MARGIN/SWAP/FUTURES/OPTION (there is no SPOT value).
        d = self._get(
            "/api/v5/account/account-position-risk",
            params={"instType": inst_type},
            auth=True,
            what="account position risk",
        )
        data = d.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise OKXBusinessError("account position risk: malformed data")
        return data[0]

    def orders_algo_pending(self, ord_type, retries=3):
        # Untriggered conditional/strategy orders of the given ordType. OKX
        # requires ``ordType`` and accepts one type per request (only
        # ``conditional`` and ``oco`` may be comma-combined). A failed query
        # raises -- it is never treated as "no orders". ``retries`` bounds the
        # transient-code retry for this specific call.
        d = self._get(
            "/api/v5/trade/orders-algo-pending",
            params={"ordType": ord_type},
            auth=True,
            what="orders algo pending",
            retries=retries,
        )
        data = d.get("data")
        if not isinstance(data, list):
            raise OKXBusinessError("orders algo pending: malformed data")
        return data

    def place_order(self, payload):
        # Single submission. No retry: an ambiguous outcome is reconciled later.
        return self.request("POST", "/api/v5/trade/order", body=payload, auth=True)

    def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
        params = {"instId": inst_id}
        if ord_id:
            params["ordId"] = ord_id
        elif cl_ord_id:
            params["clOrdId"] = cl_ord_id
        last_code = None
        for attempt in range(3):
            d = self.request("GET", "/api/v5/trade/order", params=params, auth=True)
            if not isinstance(d, dict):
                raise OKXBusinessError("order query: malformed OKX response")
            code = d.get("code")
            if code == "51603":  # "Order does not exist" — a clean not-found
                return None
            if code == "0":
                data = d.get("data") or []
                return data[0] if data else None
            last_code = code
            if code in _TRANSIENT_CODES and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise OKXBusinessError(f"order query: OKX code={code}", code=code)
        raise OKXBusinessError("order query: retries exhausted", code=last_code)

    def orders_pending(self, inst_id=None):
        # Open orders across all spot instruments when ``inst_id`` is omitted
        # (full-coverage view). The broker uses this to prove there are no
        # external open orders; a failed query raises rather than returning [].
        params = {"instId": inst_id} if inst_id else None
        d = self._get(
            "/api/v5/trade/orders-pending", params=params, auth=True, what="orders pending"
        )
        data = d.get("data")
        if not isinstance(data, list):
            raise OKXBusinessError("orders pending: malformed data")
        return data

    def _paged(self, path, params, what, key, page=100, max_pages=60):
        """Read every page of an ``ordId``/``algoId``-paginated list endpoint.

        OKX documents only ``after`` ("records earlier than the requested id")
        and a per-request maximum, with no last-page marker, so completeness is
        established by paging until an empty page arrives rather than by a short
        page. Returns ``(rows, complete)``; ``complete`` is False whenever the
        scan stopped for any reason other than an empty page, so a caller can
        never mistake a truncated scan for a full one.
        """
        rows = []
        cursor = None
        seen = set()
        for _ in range(max_pages):
            q = dict(params)
            if cursor is not None:
                q["after"] = cursor
            d = self._get(path, params=q, auth=True, what=what)
            data = d.get("data")
            if not isinstance(data, list):
                raise OKXBusinessError(f"{what}: malformed data")
            if not data:
                return rows, True
            ids = [r.get(key) for r in data]
            if any(i is None or i == "" for i in ids):
                raise OKXBusinessError(f"{what}: page item lacks {key}")
            # Paging is defined against the documented newest-first ordering. If a
            # page is not ordered that way, a cursor taken from its last row could
            # silently return nothing on the next request, which would look like a
            # complete scan of an empty range. Refuse instead.
            if not _descending(ids):
                return rows, False
            if any(i in seen for i in ids):
                return rows, False
            seen.update(ids)
            oldest = ids[-1]
            if oldest == cursor:
                # Non-advancing pagination: stop and report incompleteness rather
                # than loop or silently treat the page as the whole answer.
                return rows, False
            rows.extend(data)
            cursor = oldest
        return rows, False

    def orders_history(self, inst_type, begin=None, end=None, state=None):
        """Completed orders of an instrument type, every page.

        Retention is documented as 7 days by placement time, except that orders
        canceled without any fill are kept for only 2 hours. Returns
        ``(rows, complete)``.
        """
        params = {"instType": inst_type, "limit": "100"}
        if begin is not None:
            params["begin"] = str(begin)
        if end is not None:
            params["end"] = str(end)
        if state is not None:
            params["state"] = state
        return self._paged(
            "/api/v5/trade/orders-history", params, "orders history", "ordId"
        )

    def orders_history_archive(self, inst_type, begin=None, end=None, state=None):
        """Completed orders of the last 3 months, every page.

        Documented to exclude orders that were canceled without any fill. Returns
        ``(rows, complete)``.
        """
        params = {"instType": inst_type, "limit": "100"}
        if begin is not None:
            params["begin"] = str(begin)
        if end is not None:
            params["end"] = str(end)
        if state is not None:
            params["state"] = state
        return self._paged(
            "/api/v5/trade/orders-history-archive", params,
            "orders history archive", "ordId",
        )
