"""Shared in-memory doubles for the OKX adapter tests.

The balance-detail builder is derived from the adapter's own documented field
model, so a test double can never silently drift away from the schema the
adapter verifies. Nothing here opens a socket or reads credentials.
"""

from stonkfly.okx_broker import _LOCAL_STATE, _MUST_BE_ZERO, _NONNEGATIVE, _REQUIRED_BALANCE
from stonkfly.okx_client import OKXBusinessError

# Fields OKX reports as "" (documented "not applicable under this account
# level") on a clean cash-only account; the rest of the risk fields are "0".
_EMPTY_ON_CLEAN_ACCOUNT = frozenset({
    "liab", "crossLiab", "isoLiab", "interest", "uplLiab", "borrowFroz",
    "spotInUseAmt", "clSpotInUseAmt", "maxSpotInUse", "maxLoan", "mgnRatio",
})

# GET /api/v5/account/config for a clean demo main account in acctLv 2.
ACCOUNT_CONFIG = {
    "acctLv": "2",
    "perm": "read_only,trade",
    "uid": "uid-1",
    "mainUid": "uid-1",
    "type": "0",
    "enableSpotBorrow": False,
    "autoLoan": False,
    "spotBorrowAutoRepay": False,
}


def risk_snapshot(**extra):
    """GET /account/account-position-risk, as a clean cash-only account returns it."""
    d = {"ts": "1700000000000", "adjEq": "", "balData": [], "posData": []}
    d.update(extra)
    return d


def detail(ccy, cash, **extra):
    """One OKX balance detail row matching the live schema.

    Every field the adapter's model requires is present, encoded the way OKX
    encodes it on a clean cash-only account: "" for the fields that are not
    applicable under this account level, "0" for those that are applicable and
    clear. ``cashBal``/``availBal`` carry the balance.
    """
    d = {
        "ccy": ccy,
        "uTime": "1700000000000",
        "cashBal": str(cash),
        "availBal": str(cash),
        "eq": str(cash),
        "eqUsd": str(cash),
        "availEq": str(cash),
        "disEq": "0",
        "maxLoan": "",
        "mgnRatio": "",
        "rewardBal": "0",
        "colRes": "0",
        "colBorrAutoConversion": "0",
        "autoLendStatus": "unsupported",
        "autoStakingStatus": "unsupported",
    }
    for field in _MUST_BE_ZERO:
        d.setdefault(field, "" if field in _EMPTY_ON_CLEAN_ACCOUNT else "0")
    missing = [f for f in (*_REQUIRED_BALANCE, *_NONNEGATIVE, *_LOCAL_STATE) if f not in d]
    assert not missing, f"detail double is out of sync with the field model: {missing}"
    d.update(extra)
    return d


class OrderQueryDoubles:
    """The read-only order-query surface the broker depends on.

    Shared by every in-memory exchange double so all of them answer these
    endpoints identically, including the ``(rows, complete)`` contract that lets
    the broker tell a full scan from a truncated one. Subclasses set the lists.

    ``untriggered_algos`` models what ``GET /trade/orders-algo-pending`` returns
    (documented: "untriggered Algo orders"), filtered by the requested
    ``ordType``. ``algo_pending_transient`` names the ordTypes OKX answers 51054
    for, which is the situation this adapter must never read as "no orders".
    """

    open_orders = ()
    untriggered_algos = ()
    algo_pending_transient = ()
    history_orders = ()
    archive_orders = ()
    history_complete = True
    archive_complete = True

    def orders_pending(self, inst_id=None):
        return list(self.open_orders)

    def orders_algo_pending(self, ord_type, retries=3):
        wanted = {part.strip() for part in ord_type.split(",") if part.strip()}
        if wanted & set(self.algo_pending_transient):
            raise OKXBusinessError(
                "orders algo pending: OKX code=51054", code="51054"
            )
        return [o for o in self.untriggered_algos if o.get("ordType") in wanted]

    def orders_history(self, inst_type, begin=None, end=None, state=None):
        return list(self.history_orders), self.history_complete

    def orders_history_archive(self, inst_type, begin=None, end=None, state=None):
        return list(self.archive_orders), self.archive_complete
