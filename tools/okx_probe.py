"""Single-shot OKX public-endpoint connectivity probe.

Run from the project root with the project virtualenv's Python, passing the
proxy as an argument (it is never hardcoded):

    .venv\\Scripts\\python.exe tools\\okx_probe.py 127.0.0.1:7890

It issues exactly one unauthenticated request to OKX's public time endpoint,
routed through the given proxy. It never loads ``.env``, never calls a private
endpoint, and never prints response bodies, request headers, signatures, keys,
or account data. On success it prints only the interpreter path, the product
user-agent, the sanitized proxy host/port, the elapsed time and a success flag;
on failure it adds the HTTP status code or the underlying exception type.
"""

import os
import sys
import time
import urllib.parse
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stonkfly.okx_client import OKXClient, OKXTransportError, USER_AGENT  # noqa: E402


def sanitize_proxy(proxy):
    """Return a ``host:port`` string with any credentials stripped."""
    if not proxy:
        return "none"
    url = proxy if "://" in proxy else "http://" + proxy
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        return f"{host}:{parts.port}"
    return host


def build_client(proxy, timeout=10):
    """Build an OKXClient that routes through ``proxy`` without touching any
    system or WorkBuddy proxy setting."""
    return OKXClient(proxy=proxy, timeout=timeout)


def main(argv):
    if len(argv) != 2 or not argv[1]:
        print("usage: python tools/okx_probe.py <proxy-host:port>")
        return 2

    proxy = argv[1]
    print(f"python: {sys.executable}")
    print(f"user_agent: {USER_AGENT}")
    print(f"proxy: {sanitize_proxy(proxy)}")

    client = build_client(proxy)
    start = time.monotonic()
    try:
        ts = client.public_time()
        ok = isinstance(ts, int) and ts > 0
    except OKXTransportError as e:
        print(f"elapsed_ms: {int((time.monotonic() - start) * 1000)}")
        print("success: false")
        if e.status is not None:
            print(f"http_status: {e.status}")
        else:
            cause = e.__cause__
            print(f"cause: {type(cause).__name__ if cause is not None else type(e).__name__}")
        return 1
    except Exception as e:  # noqa: BLE001 — diagnostic probe reports type only
        print(f"elapsed_ms: {int((time.monotonic() - start) * 1000)}")
        print("success: false")
        print(f"error_type: {type(e).__name__}")
        return 1

    print(f"elapsed_ms: {int((time.monotonic() - start) * 1000)}")
    print(f"success: {'true' if ok else 'false'}")
    if not ok:
        print("error_type: MalformedTimestamp")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
