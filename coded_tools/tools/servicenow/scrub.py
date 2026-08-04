"""
Outbound text scrubbing.

Two consumers depend on this module and both are security-relevant:

  * Gateway error bodies. An upstream error page can echo an Authorization
    header, a hostname, an internal stack trace or a whole request. That text
    must never reach a log line, a progress structure, or the LLM.
  * Any free text this package emits.

The rule is deny-by-shape: anything that *looks* like a secret, a host or an
identifier is replaced, because the alternative — enumerating what is safe — fails
the first time an upstream changes its error format.
"""

import re
from typing import Any
from typing import Dict
from typing import FrozenSet
from typing import Mapping
from typing import Pattern
from typing import Tuple

REDACTED: str = "<redacted>"
DEFAULT_LIMIT: int = 500

# Order matters: the broadest credential-bearing shapes are removed first, so a
# later, narrower pattern cannot leave half a secret behind.
_PATTERNS: Tuple[Pattern[str], ...] = (
    # Authorization header values, in either scheme.
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    # Anything presenting itself as a token/secret/password in key=value form.
    # The optional quote before the separator matters: in JSON the key is quoted
    # ("client_secret": "..."), and without it only the bare form=value shape matched.
    re.compile(r"(?i)\b(?:access_token|refresh_token|id_token|client_secret|client_security"
               r"|password|passwd|secret|api[_-]?key|authorization)\b[\"']?\s*[:=]\s*"
               r"[\"']?[^\s,;&\"'}]+"),
    # Absolute URLs, which carry host and often path structure.
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+"),
    # Bare hostnames.
    re.compile(r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
               r"(?:com|net|org|io|intra|local|internal|gov|edu|cloud|dev|corp)\b"),
    # IPv4 addresses.
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    # UUIDs: client ids, record sys_ids, correlation values copied from real data.
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
               r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    # Long opaque runs: base64url, hex digests, bare tokens.
    re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}\b"),
)


def scrub_text(text: Any, limit: int = DEFAULT_LIMIT) -> str:
    """
    Redact secret-shaped and host-shaped substrings, then truncate.

    Truncation happens *after* redaction on purpose: truncating first could sever a
    token mid-string and leave a fragment that no longer matches its pattern.

    :param text: Any value; non-strings are coerced.
    :param limit: Maximum length of the returned string.
    :return: A scrubbed, length-bounded string.
    """
    if text is None:
        return ""
    scrubbed: str = str(text)
    for pattern in _PATTERNS:
        scrubbed = pattern.sub(REDACTED, scrubbed)
    scrubbed = " ".join(scrubbed.split())
    if len(scrubbed) > limit:
        scrubbed = scrubbed[:limit] + "…"
    return scrubbed


def scrub_mapping(mapping: Mapping[str, Any], limit: int = DEFAULT_LIMIT,
                  safe_keys: FrozenSet[str] = frozenset()) -> Dict[str, Any]:
    """
    Scrub every string leaf of a mapping, preserving structure.

    :param mapping: The mapping to scrub.
    :param limit: Per-value length bound.
    :param safe_keys: Keys whose values pass through unscrubbed, at any depth.
                      Exists because the shape patterns cannot tell a secret from
                      an identifier we mint ourselves: a correlation id and a
                      payload hash are long opaque alphanumerics, exactly the shape
                      of a leaked token — and without this exemption the scrubber
                      redacted the very join key the audit trail exists to carry.
                      Callers name each exempt key deliberately; nothing is exempt
                      by default.
    :return: A new dictionary with scrubbed values.
    """
    result: Dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(value, Mapping):
            result[key] = scrub_mapping(value, limit, safe_keys)
        elif str(key) in safe_keys and isinstance(value, str):
            result[key] = value[:limit]
        elif isinstance(value, (list, tuple)):
            result[key] = [scrub_text(item, limit) if isinstance(item, str) else item
                           for item in value]
        elif isinstance(value, str):
            result[key] = scrub_text(value, limit)
        else:
            result[key] = value
    return result
