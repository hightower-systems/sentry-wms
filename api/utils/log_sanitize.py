"""
Scrub credential fragments from text before logging or persisting to
sync_state.last_error_message.

URL userinfo and sensitive query parameters: a URL like
https://user:pass@host/path?api_key=SECRET is decomposed via urlparse
so userinfo and known credential-shaped query keys are redacted
without mangling the rest of the URL.

Credential pattern catalog (#52): connectors do not always raise an
exception that embeds a URL. A RuntimeError("API rejected key
'sk-abc123' for account 42") would slip through URL-only scrubbing.
CREDENTIAL_PATTERNS catches the credential shapes seen on the
connector surface today: Sentry's own bearer tokens, AWS access keys,
generic Bearer headers, key=value connection-string fragments,
NetSuite OAuth fragments, JWT-shaped strings, plus a heuristic
catch-all for long base64-ish strings near credential keywords.

Pattern false positives are acceptable: a long base64-ish string near
the word "key" but not actually a credential gets redacted. Defence
favours over-redaction for log content. New connector types that
introduce a new credential shape add a pattern entry rather than a
new helper.

scrub_secrets is idempotent: applying twice yields the same result as
applying once.
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

    if parsed.username or parsed.password:
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
        changed = True

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


def _scrub_urls(text: str) -> str:
    return _URL_RE.sub(_scrub_one_url, text)


def _scrub_long_credential_after_keyword(m: "re.Match") -> str:
    return m.group(0).replace(m.group(1), "<REDACTED>")


CREDENTIAL_PATTERNS = [
    (re.compile(r"wms_t_[A-Za-z0-9_\-]{20,}"),
     "wms_t_<REDACTED>"),

    (re.compile(r"AKIA[0-9A-Z]{16}"),
     "AKIA<REDACTED>"),

    (re.compile(r"(?i)Bearer\s+[A-Za-z0-9_.\-=]{20,}"),
     "Bearer <REDACTED>"),

    (re.compile(r"(?i)\b(password|pwd|passwd|secret|api[_-]?key|token|auth)=([^;&\s]+)"),
     r"\1=<REDACTED>"),

    (re.compile(r'(?i)oauth_(?:token|signature|consumer_key|consumer_secret)["\s=:]+[A-Za-z0-9%+/=_\-]{16,}'),
     "oauth_<REDACTED>"),

    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
     "<JWT_REDACTED>"),

    (re.compile(r"(?i)(?:key|token|secret|password)\b[^a-z0-9]{1,32}([A-Za-z0-9+/=_\-]{32,})"),
     _scrub_long_credential_after_keyword),
]


def scrub_credentials(text: str) -> str:
    """Apply every credential pattern in order. Idempotent."""
    if not text:
        return text
    for pattern, replacement in CREDENTIAL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrub_secrets(text) -> str:
    """Return ``text`` with URL userinfo, sensitive query values, and
    known credential fragments redacted.

    ``text`` may be anything str-convertible. None -> empty string.
    """
    if text is None:
        return ""
    s = str(text)
    if not s:
        return s
    s = _scrub_urls(s)
    s = scrub_credentials(s)
    return s
