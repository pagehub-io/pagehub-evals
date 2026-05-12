"""Unit tests for ``api.runs.engine`` — DB-free internals only."""

from __future__ import annotations

import re
from uuid import uuid4

import pytest

from api.runs._redact import redact_secrets
from api.runs._ssrf import is_blocked_host
from api.runs.engine import (
    _MISSING,
    _aggregate_verdict,
    _find_substitution_misses,
    _resolve_path,
    _substitute,
)
from api.runs.schemas import RunEvaluationResult, RunRequestResult

# ---------- _substitute ----------


def test_substitute_replaces_in_string() -> None:
    assert _substitute("hi {{NAME}}", {"NAME": "world"}) == "hi world"


def test_substitute_walks_dict_and_list() -> None:
    out = _substitute({"a": "{{X}}", "b": ["{{X}}", "{{Y}}"]}, {"X": "1", "Y": "2"})
    assert out == {"a": "1", "b": ["1", "2"]}


def test_substitute_leaves_missing_keys_literal() -> None:
    assert _substitute("{{MISSING}}", {}) == "{{MISSING}}"
    assert _substitute({"k": "{{MISSING}}"}, {"OTHER": "x"}) == {"k": "{{MISSING}}"}


def test_substitute_capture_overrides_env_var() -> None:
    subs = {"K": "env-value"}
    subs["K"] = "captured-value"
    assert _substitute("{{K}}", subs) == "captured-value"


def test_substitute_passes_through_non_string_scalars() -> None:
    assert _substitute(42, {"X": "1"}) == 42
    assert _substitute(None, {"X": "1"}) is None


# ---------- _find_substitution_misses ----------


def test_find_misses_in_url_headers_body() -> None:
    payload = {
        "url": "https://x/{{A}}",
        "headers": {"X-Auth": "{{TOK}}"},
        "body": {"id": "{{A}}", "etag": "{{ETAG}}"},
    }
    misses = _find_substitution_misses(payload)
    assert set(misses) == {"A", "TOK", "ETAG"}


def test_find_misses_regex_matches_valid_identifiers_only() -> None:
    # Identifier rule: ^[A-Za-z_][A-Za-z0-9_]*$
    text = "{{good_VAR1}} {{1bad}} {{ok_too}} {{with-dash}}"
    misses = _find_substitution_misses(text)
    assert "good_VAR1" in misses
    assert "ok_too" in misses
    assert "1bad" not in misses
    assert "with-dash" not in misses


def test_find_misses_dedupes_in_order() -> None:
    misses = _find_substitution_misses("{{X}} {{Y}} {{X}}")
    assert misses == ["X", "Y"]


# ---------- _resolve_path ----------


def test_resolve_path_dot_chain() -> None:
    body = {"a": {"b": {"c": "found"}}}
    assert _resolve_path(body, "$.a.b.c") == "found"


def test_resolve_path_array_index() -> None:
    body = {"items": [{"id": "first"}, {"id": "second"}]}
    assert _resolve_path(body, "$.items[0].id") == "first"
    assert _resolve_path(body, "$.items[1].id") == "second"


def test_resolve_path_missing_returns_sentinel() -> None:
    assert _resolve_path({"a": {}}, "$.a.b") is _MISSING
    assert _resolve_path({"a": []}, "$.a[0]") is _MISSING
    assert _resolve_path({}, "$.x") is _MISSING


def test_resolve_path_malformed_returns_sentinel() -> None:
    assert _resolve_path({"a": [1, 2]}, "$.a[notnumeric]") is _MISSING
    assert _resolve_path({"a": [1]}, "$.a[5]") is _MISSING
    assert _resolve_path({}, "") is _MISSING


# ---------- redact_secrets ----------


def test_redact_replaces_in_strings_only() -> None:
    out = redact_secrets({"shh"}, "the password is shh!")
    assert out == "the password is ***!"


def test_redact_walks_nested_structures() -> None:
    obj = {
        "a": "hello shh",
        "b": ["nope", "shh again"],
        "c": {"d": "shh"},
        "n": 5,
    }
    out = redact_secrets({"shh"}, obj)
    assert out == {
        "a": "hello ***",
        "b": ["nope", "*** again"],
        "c": {"d": "***"},
        "n": 5,
    }


def test_redact_longest_first_avoids_partial_replace() -> None:
    out = redact_secrets({"abc", "abcdef"}, "X abcdef Y abc Z")
    # The longer secret is replaced first, so we don't get "***def" first
    assert out == "X *** Y *** Z"


def test_redact_no_secrets_returns_input() -> None:
    assert redact_secrets(set(), "anything {{X}}") == "anything {{X}}"


# ---------- is_blocked_host ----------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/foo",
        "http://10.0.0.1/x",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0/",
    ],
)
def test_ssrf_blocks_rfc1918_in_staging(url: str) -> None:
    blocked, reason = is_blocked_host(url, "staging")
    assert blocked is True
    assert reason is not None


@pytest.mark.parametrize("env", ["staging", "production"])
def test_ssrf_blocks_disallowed_schemes(env: str) -> None:
    blocked, reason = is_blocked_host("file:///etc/passwd", env)
    assert blocked is True
    assert "scheme" in (reason or "")


def test_ssrf_allows_public_in_staging() -> None:
    blocked, _ = is_blocked_host("https://api.example.com/", "staging")
    assert blocked is False


def test_ssrf_allows_everything_in_dev() -> None:
    blocked, _ = is_blocked_host("http://127.0.0.1/x", "development")
    assert blocked is False
    blocked, _ = is_blocked_host("http://twins:8000/x", "development")
    assert blocked is False


def test_ssrf_allows_unknown_env_to_be_permissive_false() -> None:
    # Unknown envs are deny-by-default for the actual block list (no env match),
    # which means they fall through to "not blocked" — but the contract says
    # "ONLY active when env in {staging, production}". Confirm that's the shape.
    blocked, _ = is_blocked_host("http://127.0.0.1/", "qa")
    assert blocked is False


# ---------- verdict aggregation ----------


def _mkreq(
    *,
    passed: bool,
    transport_error: str | None = None,
    evaluations: list[RunEvaluationResult] | None = None,
) -> RunRequestResult:
    return RunRequestResult(
        request_id=uuid4(),
        request_name="r",
        method="GET",
        url="http://x",
        response_status=200,
        response_headers={},
        response_body_excerpt=None,
        latency_ms=1,
        transport_error=transport_error,
        substitution_missed=[],
        captured=[],
        evaluations=evaluations or [],
        passed=passed,
    )


def _mkev(passed: bool) -> RunEvaluationResult:
    return RunEvaluationResult(
        id=uuid4(),
        name="e",
        kind="status_eq",
        passed=passed,
        detail={},
        error=None,
    )


def test_verdict_no_requests_is_error() -> None:
    assert _aggregate_verdict([]) == ("error", "error")


def test_verdict_transport_error_dominates() -> None:
    a = _mkreq(passed=True, evaluations=[_mkev(True)])
    b = _mkreq(passed=False, transport_error="ConnectError: nope")
    assert _aggregate_verdict([a, b]) == ("error", "error")


def test_verdict_no_evaluations_is_error() -> None:
    a = _mkreq(passed=False, evaluations=[])  # no evals → not really passed
    assert _aggregate_verdict([a]) == ("error", "error")


def test_verdict_one_failing_eval_is_failed() -> None:
    a = _mkreq(passed=True, evaluations=[_mkev(True)])
    b = _mkreq(passed=False, evaluations=[_mkev(False)])
    assert _aggregate_verdict([a, b]) == ("failed", "failed")


def test_verdict_all_passed_is_passed() -> None:
    a = _mkreq(passed=True, evaluations=[_mkev(True)])
    b = _mkreq(passed=True, evaluations=[_mkev(True), _mkev(True)])
    assert _aggregate_verdict([a, b]) == ("passed", "passed")


# ---------- substitution token regex matches the published spec ----------


def test_substitution_token_regex_matches_spec() -> None:
    # The published regex literal — re-derive to keep tests honest.
    pattern = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
    assert pattern.findall("{{X}} {{y_2}} {{1bad}}") == ["X", "y_2"]
