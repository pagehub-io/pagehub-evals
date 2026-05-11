"""Secret-value redaction for persisted evidence.

Contract: substring-replace any value in ``secret_values`` with
``"***"`` inside strings encountered while walking ``obj`` (dicts and
lists are descended; other types pass through unchanged). Operates by
returning a new structure so the caller can rebind the result; the
input is not mutated.

Only strings are touched. Non-string scalars (ints, bools, None) are
returned as-is. This matches the PLAN's "Strings only — never coerce
other types" rule.
"""

from __future__ import annotations

from typing import Any

_REDACTED = "***"


def redact_secrets(secret_values: set[str], obj: Any) -> Any:
    """Return a copy of ``obj`` with any ``secret_values`` substrings replaced."""
    if not secret_values:
        return obj
    return _walk(secret_values, obj)


def _walk(secret_values: set[str], obj: Any) -> Any:
    if isinstance(obj, str):
        return _redact_str(secret_values, obj)
    if isinstance(obj, dict):
        return {k: _walk(secret_values, v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(secret_values, v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_walk(secret_values, v) for v in obj)
    return obj


def _redact_str(secret_values: set[str], s: str) -> str:
    # Replace longest secrets first — protects against a short secret
    # that's a substring of a longer one being half-redacted.
    out = s
    for secret in sorted(secret_values, key=len, reverse=True):
        if not secret:
            continue
        if secret in out:
            out = out.replace(secret, _REDACTED)
    return out
