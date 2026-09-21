"""Boundary tests for the client's paginated read-only list endpoints.

These exist because every completeness claim in the audit and migration rests on
``(rows, complete)``: a scan that stopped early must never be presented as an
empty account, and the failure modes that would cause that are all offline here.
"""

import urllib.parse

import pytest

from stonkfly.okx_client import OKXBusinessError, OKXClient


def client(pages):
    """A client whose transport serves pages keyed by the ``after`` cursor."""

    def transport(method, url, headers, data):
        query = urllib.parse.urlparse(url).query
        after = urllib.parse.parse_qs(query).get("after", [None])[0]
        return 200, {"code": "0", "data": pages.get(after, [])}

    return OKXClient(api_key="K", secret="S", passphrase="P", transport=transport)


def rows(*ids):
    return [{"ordId": i} for i in ids]


def test_paging_reads_every_page_until_an_empty_one():
    c = client({None: rows("100", "99"), "99": rows("98"), "98": []})
    result, complete = c.orders_history("SPOT")
    assert complete is True
    assert [r["ordId"] for r in result] == ["100", "99", "98"]


def test_a_short_page_is_not_treated_as_the_last_page():
    # OKX documents no last-page marker, so a page smaller than the maximum is not
    # evidence of the end: only an empty page is.
    c = client({None: rows("100"), "100": rows("99"), "99": []})
    result, complete = c.orders_history("SPOT")
    assert complete is True
    assert [r["ordId"] for r in result] == ["100", "99"]


def test_a_page_out_of_documented_order_is_incomplete_not_complete():
    # If ordering slipped, a cursor taken from the last row could return nothing
    # and look like a finished scan of an empty range.
    c = client({None: rows("99", "100")})
    result, complete = c.orders_history("SPOT")
    assert complete is False
    assert result == []


def test_a_repeated_page_is_incomplete():
    c = client({None: rows("100"), "100": rows("100")})
    _result, complete = c.orders_history("SPOT")
    assert complete is False


def test_a_cursor_that_does_not_advance_is_incomplete():
    c = client({None: rows("100", "99"), "99": rows("99")})
    _result, complete = c.orders_history("SPOT")
    assert complete is False


def test_exhausting_the_page_budget_is_incomplete():
    # Never claim completeness just because we stopped asking.
    def transport(method, url, headers, data):
        query = urllib.parse.urlparse(url).query
        after = urllib.parse.parse_qs(query).get("after", ["1000"])[0]
        return 200, {"code": "0", "data": rows(str(int(after) - 1))}

    c = OKXClient(api_key="K", secret="S", passphrase="P", transport=transport)
    result, complete = c._paged("/x", {}, "x", "ordId", max_pages=3)
    assert complete is False
    assert len(result) == 3


def test_a_malformed_row_is_refused_not_skipped():
    # A row without the paging key would make the cursor meaningless.
    c = client({None: [{"ordId": "100"}, {}]})
    with pytest.raises(OKXBusinessError):
        c.orders_history("SPOT")


def test_a_failed_page_raises_rather_than_truncating(monkeypatch):
    calls = []

    def transport(method, url, headers, data):
        calls.append(url)
        if len(calls) == 1:
            return 200, {"code": "0", "data": rows("100")}
        return 200, {"code": "51054", "msg": "Request timed out. Please try again.", "data": []}

    monkeypatch.setattr("time.sleep", lambda _s: None)
    c = OKXClient(api_key="K", secret="S", passphrase="P", transport=transport)
    with pytest.raises(OKXBusinessError) as ei:
        c.orders_history("SPOT")
    assert ei.value.code == "51054"


def test_archive_scan_uses_the_same_completeness_contract():
    c = client({None: [{"ordId": "5"}], "5": []})
    result, complete = c.orders_history_archive("SPOT")
    assert complete is True
    assert [r["ordId"] for r in result] == ["5"]


# ---------------------------------------------------------------------------
# Keep-alive transport: reuse, stale-connection retry, POST isolation.
# ---------------------------------------------------------------------------

class FakeConnection:
    """HTTP(S) connection double: scripted responses, connect counting."""

    def __init__(self, script):
        self.script = list(script)   # per request: response or exception
        self.connects = 0
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        pass

    def getresponse(self):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item

        class R:
            status = 200

            def read(self):
                return b'{"code": "0", "data": []}'

        return R()

    def close(self):
        self.closed = True

    def set_tunnel(self, host, port, headers=None):
        pass


def test_pooled_transport_reuses_one_connection():
    conns = []

    def factory(url):
        c = FakeConnection([None, None])
        conns.append(c)
        return c

    from stonkfly.okx_client import KeepAliveTransport

    transport = KeepAliveTransport(10, connection_factory=factory)
    transport("GET", "https://www.okx.com/api/v5/public/time", {}, None)
    transport("GET", "https://www.okx.com/api/v5/public/time", {}, None)
    assert len(conns) == 1              # one connection for both reads
    assert not conns[0].closed


def test_pooled_get_retries_once_on_a_stale_connection():
    conns = []

    def factory(url):
        # the first connection ever is stale; the re-established one works
        c = FakeConnection([OSError("stale")] if not conns else [None])
        conns.append(c)
        return c

    from stonkfly.okx_client import KeepAliveTransport

    transport = KeepAliveTransport(10, connection_factory=factory)
    status, data = transport("GET", "https://www.okx.com/api/v5/public/time", {}, None)
    assert status == 200 and data["code"] == "0"
    assert len(conns) == 2               # the stale one + the re-established one
    assert conns[0].closed and not conns[1].closed


def test_post_takes_a_fresh_connection_and_never_retries():
    conns = []

    def factory(url):
        c = FakeConnection([OSError("dead"), None])
        conns.append(c)
        return c

    from stonkfly.okx_client import KeepAliveTransport

    transport = KeepAliveTransport(10, connection_factory=factory)
    try:
        transport("POST", "https://www.okx.com/api/v5/trade/order", {}, b"{}")
        raise AssertionError("a failed POST must surface, not retry")
    except OSError:
        pass
    assert len(conns) == 1               # exactly one attempt, no second
    assert conns[0].closed               # the fresh connection is closed after


def test_transport_proxy_is_recorded():
    from stonkfly.okx_client import KeepAliveTransport

    transport = KeepAliveTransport(10, proxy="127.0.0.1:7890")
    assert transport.proxy == "127.0.0.1:7890"
    transport = KeepAliveTransport(10, proxy=None)
    # no explicit proxy: resolved from the environment at use time
    assert transport.proxy is None or isinstance(transport.proxy, str)
