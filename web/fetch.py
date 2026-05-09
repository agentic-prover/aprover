"""
Server-side URL fetching for the AProver chat tool.

Lets the agent pull C source from GitHub raw URLs (or anywhere else returning
plain text) so users can verify code by pasting a link instead of the file.

Safety constraints:
- http(s) only
- block loopback / RFC1918 / link-local / IPv6 ULA hosts (basic SSRF guard)
- 64KB size cap (matches the runner's source-size cap)
- 15s timeout
- normalize github.com/<owner>/<repo>/blob/<ref>/<path> to raw.githubusercontent.com
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

_MAX_BYTES = 64 * 1024
_TIMEOUT = 15
_USER_AGENT = "aprover-web/0.1 (+https://github.com/agentic-prover/aprover)"

_PRIVATE_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_host(host: str) -> bool:
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        for net in _PRIVATE_NETS:
            if ip.version == net.version and ip in net:
                return True
    return False


def _normalize(url: str) -> str:
    """Rewrite well-known forge URLs to their raw equivalents."""
    parts = urllib.parse.urlparse(url.strip())
    if parts.netloc == "github.com" and "/blob/" in parts.path:
        new_path = parts.path.replace("/blob/", "/", 1)
        return urllib.parse.urlunparse(
            ("https", "raw.githubusercontent.com", new_path, "", "", "")
        )
    if parts.netloc.endswith("gitlab.com") and "/-/blob/" in parts.path:
        new_path = parts.path.replace("/-/blob/", "/-/raw/", 1)
        return urllib.parse.urlunparse(
            ("https", parts.netloc, new_path, "", parts.query, "")
        )
    return url.strip()


def fetch_source(url: str) -> tuple[bool, str]:
    """Fetch text content from ``url``. Returns ``(ok, content_or_error)``."""
    if not url or not url.strip():
        return False, "Empty URL."
    target = _normalize(url)
    parts = urllib.parse.urlparse(target)
    if parts.scheme not in ("http", "https"):
        return False, f"Unsupported scheme: {parts.scheme!r} (only http/https)."
    if not parts.netloc:
        return False, "URL has no host."
    if _is_private_host(parts.hostname or ""):
        return False, f"Refusing to fetch from private/loopback host: {parts.hostname}"

    req = urllib.request.Request(
        target,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/plain, */*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 — scheme already vetted
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) > _MAX_BYTES:
                return False, f"Content too large ({cl} bytes; cap is {_MAX_BYTES})."
            data = resp.read(_MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                return False, f"Content too large (truncated at {_MAX_BYTES} bytes)."
            try:
                return True, data.decode("utf-8")
            except UnicodeDecodeError:
                return False, "Content is not valid UTF-8 text."
    except HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.reason}"
    except URLError as exc:
        return False, f"Network error: {exc.reason}"
    except (TimeoutError, socket.timeout):
        return False, "Fetch timed out."
    except Exception as exc:  # pragma: no cover
        return False, f"{type(exc).__name__}: {exc}"
