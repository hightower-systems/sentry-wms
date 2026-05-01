"""Single source of truth for delivery_url canonicalization.

The URL-reuse tombstone gate (#218) needs case / port / fragment /
trailing-slash variants to collapse to one key so a one-character
mutation cannot reuse a deleted subscription's delivery_url without
the acknowledge_url_reuse step. The PATCH path (#219) reuses the
same helper so changing a subscription's delivery_url cannot bypass
the gate either.

The canonical form is stored alongside the raw URL on every
tombstone INSERT and queried via the partial unique index on
delivery_url_canonical. The raw column stays for forensic
recall ("which URL did the admin actually type?"); the canonical
column is the one the gate compares.

Out of scope: punycode / IDN homograph normalization. The dispatch-
time SSRF guard resolves the host fresh on every POST, so a
homograph URL fails at the wire-send step rather than at the
tombstone match step.
"""

from urllib.parse import urlsplit, urlunsplit


_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonicalize_delivery_url(url: str) -> str:
    """Return the canonical form of a webhook delivery URL.

    Rules (in order):
      1. Lowercase the scheme.
      2. Lowercase the host.
      3. Drop the port when it matches the scheme's default
         (443 for https, 80 for http).
      4. Drop the URL fragment ('#...'); never sent on the wire.
      5. Empty path becomes '/'.
      6. Strip a single trailing '/' from non-root paths so
         '/hook' and '/hook/' collapse.

    The query string is preserved verbatim. Userinfo
    ('user:pass@') is preserved verbatim too; the SSRF guard
    inspects the resolved host, not the URL string.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()

    host = (parts.hostname or "").lower()
    port = parts.port
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None

    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo += ":" + parts.password
        userinfo += "@"

    netloc = userinfo + host
    if port is not None:
        netloc += ":" + str(port)

    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"

    return urlunsplit((scheme, netloc, path, parts.query, ""))
