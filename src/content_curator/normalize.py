"""URL normalization and content hashing for exact deduplication."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only track the click and never change the content.
TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid",
    "ref_src", "ref_url", "_hsenc", "_hsmi", "mkt_tok", "cmpid", "s_kwcid", "spm",
}
TRACKING_PREFIXES = ("utm_", "pk_", "mtm_")
DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str) -> str:
    """Return a stable form of a URL so trivially different links compare equal.

    Lowercases scheme and host, drops "www.", default ports, fragments and tracking
    parameters, sorts the remaining query, and strips a trailing slash from the path.
    http and https are treated as the same document.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme == "http":
        scheme = "https"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host if port is None or port == DEFAULT_PORTS.get(parts.scheme.lower()) else f"{host}:{port}"

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    for suffix in ("/index.html", "/index.htm", "/index.php"):
        if path.endswith(suffix):
            path = path[: -len(suffix)] or "/"

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
    ]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def content_hash(text: str | None, min_words: int = 100) -> str | None:
    """Hash of whitespace- and case-normalized text, or None when too short to be distinctive."""
    if not text:
        return None
    words = re.findall(r"\w+", text.lower())
    if len(words) < min_words:
        return None
    return hashlib.sha256(" ".join(words).encode()).hexdigest()


def word_count(text: str | None) -> int:
    return len(re.findall(r"\w+", text)) if text else 0
