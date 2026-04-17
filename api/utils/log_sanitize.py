"""
V-007: scrub credential fragments from error text before logging or
persisting to sync_state.last_error_message.

Primary target: URL userinfo (https://user:pass@host). If any connector
ever builds such a URL and the request fails, the exception message
often embeds the whole URL verbatim. urlparse separates the userinfo
cleanly so we can redact it without mangling the rest.

Secondary target: common credential-like query parameters
(api_key, token, access_token, secret, password). Values are replaced
with "REDACTED". This is best-effort; it does not replace the
guarantee that credentials should live in Authorization headers, not
URLs.
"""

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_URL_RE = re.compile(r"https?://[^\s<>\"'\\]+")

_SENSITIVE_QUERY_KEYS = frozenset({
    "api_key", "apikey", "api-key",
    "token", "access_token", "refresh_token",
    "secret", "client_secret",
    "password", "pwd",
    "authorization", "auth",
})


def _scrub_one_url(match: "re.Match") -> str:
    original = match.group(0)
    try:
        parsed = urlparse(original)
    except Exception:
        return "***REDACTED-URL***"

    changed = False

    # Drop userinfo (username[:password]) entirely.
    if parsed.username or parsed.password:
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
        changed = True

    # Redact sensitive query parameter values.
    if parsed.query:
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        new_pairs = []
        for k, v in pairs:
            if k.lower() in _SENSITIVE_QUERY_KEYS:
                new_pairs.append((k, "REDACTED"))
                changed = True
            else:
                new_pairs.append((k, v))
        if changed:
            parsed = parsed._replace(query=urlencode(new_pairs, doseq=True))

    if not changed:
        return original
    return urlunparse(parsed)


def scrub_secrets(text) -> str:
    """Return ``text`` with URL userinfo and sensitive query values stripped.

    ``text`` may be anything str-convertible. None -> empty string.
    """
    if text is None:
        return ""
    s = str(text)
    if not s:
        return s
    return _URL_RE.sub(_scrub_one_url, s)
