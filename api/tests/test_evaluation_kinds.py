"""Unit tests for the four path-bearing evaluation kinds, the filter capture
form, config rendering, and the redact-then-bound pass. No DB, no HTTP."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.environments.schemas import CreateEnvironmentRequest, UpdateEnvironmentRequest
from api.evaluations.schemas import CreateEvaluationRequest
from api.fixtures.schemas import FixtureEnvironment, FixtureRequest
from api.requests.schemas import CreateRequestRequest
from api.runs import engine as engine_mod
from api.runs.engine import _KINDS, _MISSING, _redact_result_in_place, _render_config, _resolve_path
from api.shared.jsonpath import JSON_PATH_LITE_RE, is_valid_json_path

BODY = {
    "access_token": "tok",
    "count": 3,
    "ratio": 1.5,
    "flag": True,
    "nothing": None,
    "items": [{"name": "a"}, 7, {"name": "default", "id": "r-2"}],
    "text": "hello world",
    "obj": {"k": "v"},
}


# ---------- grammar ----------

@pytest.mark.parametrize(
    "path",
    ["$.a", "$.a.b[1]", "$[0]", "$.items[-1].name", "$.roles[?(@.name=='default')].id",
     '$.roles[?(@.name=="x y")]', "$.a[?(@.k=='a]b')]"],
)
def test_grammar_accepts(path: str) -> None:
    assert is_valid_json_path(path)


@pytest.mark.parametrize(
    "path",
    ["$", "$.", "$..", "$a", "$.a[]", "$.a[x]", "$.a[0", "$.a.", "$.a..b", "$['a']", "a.b",
     "$.a ", "$. a", "$.a\n", "$.a[?(@.x='1')", "$.a[?(@.x=1)]", "$.a[?(@.x y=='1')]"],
)
def test_grammar_rejects(path: str) -> None:
    assert not is_valid_json_path(path)


def test_grammar_and_tokenizer_agree_on_filter_with_bracket_in_literal() -> None:
    body = {"a": [{"k": "a]b", "v": 1}, {"k": "c", "v": 2}]}
    path = "$.a[?(@.k=='a]b')].v"
    assert JSON_PATH_LITE_RE.fullmatch(path)
    assert _resolve_path(body, path) == 1


# ---------- filter captures ----------

def test_filter_first_match_and_double_quotes() -> None:
    assert _resolve_path(BODY, "$.items[?(@.name=='default')].id") == "r-2"
    assert _resolve_path(BODY, '$.items[?(@.name=="default")].id') == "r-2"


def test_filter_skips_non_object_elements_and_no_coercion() -> None:
    assert _resolve_path(BODY, "$.items[?(@.name=='7')]") is _MISSING
    assert _resolve_path({"a": [{"k": None}]}, "$.a[?(@.k=='None')]") is _MISSING


def test_filter_on_non_list_or_no_match_is_missing() -> None:
    assert _resolve_path(BODY, "$.obj[?(@.k=='v')]") is _MISSING
    assert _resolve_path(BODY, "$.items[?(@.name=='zzz')]") is _MISSING


# ---------- kinds ----------

def _run(kind: str, config: dict, body=BODY, status: int = 200) -> tuple[bool, dict]:
    return _KINDS[kind](status, body, {}, config)


def test_exists_hit_miss_and_null() -> None:
    ok, d = _run("json_path_exists", {"path": "$.access_token"})
    assert ok and d == {"path": "$.access_token", "missing": False, "observed_type": "string"}
    ok, d = _run("json_path_exists", {"path": "$.nothing"})
    assert ok and d["observed_type"] == "null"
    ok, d = _run("json_path_exists", {"path": "$.absent"})
    assert not ok and d == {"path": "$.absent", "missing": True, "observed_type": None}


def test_not_exists() -> None:
    ok, d = _run("json_path_not_exists", {"path": "$.absent"})
    assert ok and d["missing"] is True
    ok, d = _run("json_path_not_exists", {"path": "$.obj"})
    assert not ok and d["observed_type"] == "object"


def test_contains_string_and_nonstring() -> None:
    # A string is searched directly.
    ok, d = _run("json_path_contains", {"path": "$.text", "needle": "lo wo"})
    assert ok and d["found"] and d["observed"] == "hello world" and d["needle_raw"] == "lo wo"
    ok, d = _run("json_path_contains", {"path": "$.text", "needle": "zzz"})
    assert not ok and d["found"] is False and d["missing"] is False
    # A non-string is rendered with Python ``str()`` and searched, EXACTLY as the
    # platform operator (``expected in str(actual)``). Array membership works via
    # the bare element, number via its digits; evidence == the searched haystack.
    ok, d = _run("json_path_contains", {"path": "$.items", "needle": "default"},
                 body={"items": ["default", "care_team"]})
    assert ok and d["found"] and d["observed_type"] == "array"
    assert d["observed"] == "['default', 'care_team']"  # str(), not json.dumps
    ok, d = _run("json_path_contains", {"path": "$.items", "needle": "absent"},
                 body={"items": ["default"]})
    assert not ok and d["found"] is False
    ok, d = _run("json_path_contains", {"path": "$.count", "needle": "3"})
    assert ok and d["observed_type"] == "number" and d["observed"] == "3"
    ok, d = _run("json_path_contains", {"path": "$.absent", "needle": "x"})
    assert not ok and d["missing"] is True and d["observed_type"] is None


def test_contains_is_exact_str_parity_with_platform() -> None:
    # Pin the parity claim itself: for every non-string type the engine result
    # must equal ``needle in str(actual)`` — the verbatim platform operator.
    # This is the case json.dumps rendering silently flipped (true/True,
    # null/None, container quoting).
    cases = [
        ({"v": True}, "True"), ({"v": True}, "true"),
        ({"v": False}, "False"), ({"v": False}, "false"),
        ({"v": None}, "None"), ({"v": None}, "null"),
        ({"v": ["default", "care_team"]}, "'default'"),
        ({"v": ["default", "care_team"]}, '"default"'),
        ({"v": {"name": "default"}}, "'name'"),
        ({"v": 1.0}, "1.0"), ({"v": 1.0}, "1"),
    ]
    for body, needle in cases:
        ok, d = _run("json_path_contains", {"path": "$.v", "needle": needle}, body=body)
        expected = needle in str(body["v"])
        assert ok is expected, f"{body['v']!r} contains {needle!r}: engine={ok} platform={expected}"
        assert d["observed"] == str(body["v"])


def test_cmp_float_coercion_parity() -> None:
    # Real numbers compare.
    assert _run("json_path_cmp", {"path": "$.count", "op": "gte", "expected": 3})[0]
    assert _run("json_path_cmp", {"path": "$.ratio", "op": "gt", "expected": 1})[0]
    assert not _run("json_path_cmp", {"path": "$.count", "op": "lt", "expected": 3})[0]
    # Platform parity (`a, e = float(actual), float(expected)`): a JSON string
    # number coerces and compares — the case the number-only guard used to flip.
    ok, d = _run("json_path_cmp", {"path": "$.v", "op": "gt", "expected": 20}, body={"v": "30"})
    assert ok and d["observed_type"] == "string" and d["observed"] == "30"
    # A bool coerces (float(True) == 1.0), like the platform.
    ok, d = _run("json_path_cmp", {"path": "$.flag", "op": "gte", "expected": 1}, body={"flag": True})
    assert ok and d["observed_type"] == "boolean"
    # Non-coercible values fail the comparison (float() raises → uncomparable).
    ok, d = _run("json_path_cmp", {"path": "$.v", "op": "gt", "expected": 1}, body={"v": "abc"})
    assert not ok and d.get("uncomparable") and d["observed"] is None
    ok, d = _run("json_path_cmp", {"path": "$.v", "op": "gt", "expected": 1}, body={"v": [1, 2]})
    assert not ok and d.get("uncomparable")
    # Missing path.
    ok, d = _run("json_path_cmp", {"path": "$.absent", "op": "gte", "expected": 1})
    assert not ok and d["missing"] is True


def test_non_json_body_is_a_string() -> None:
    ok, d = _run("json_path_exists", {"path": "$.x"}, body="<html>502</html>")
    assert not ok and d["missing"] is True


# ---------- config rendering ----------

def test_render_config_named_fields_only() -> None:
    subs = {"USER_ID": "u-1", "RUN_ID": "abc"}
    cfg, missed = _render_config("json_path_eq", {"path": "$.{{USER_ID}}", "expected": "{{USER_ID}}"}, subs)
    assert cfg["expected"] == "u-1" and cfg["expected_raw"] == "{{USER_ID}}"
    assert cfg["path"] == "$.{{USER_ID}}"  # never rendered
    assert missed == []
    cfg, missed = _render_config("body_contains", {"needle": "org-{{RUN_ID}}-{{NOPE}}"}, subs)
    assert cfg["needle"] == "org-abc-{{NOPE}}" and missed == ["NOPE"]
    cfg, missed = _render_config("json_path_eq", {"path": "$.n", "expected": 5}, subs)
    assert cfg["expected"] == 5 and "expected_raw" not in cfg


# ---------- redact then bound ----------

def _result(**over):
    base = {
        "url": "http://x/?k=tok_sssss",
        "response_headers": {"h": "tok_sssss"},
        "response_body_excerpt": None,
        "transport_error": None,
        "evaluations": [],
        "_error_suffix": "",
    }
    base.update(over)
    return base


def test_secret_straddling_excerpt_cut_is_masked() -> None:
    secret = "SECRET-abc"
    r = _result(response_body_excerpt="x" * 996 + secret + "tail")
    _redact_result_in_place(r, {secret})
    assert secret[:4] not in r["response_body_excerpt"][990:]
    assert "***" in r["response_body_excerpt"]
    assert len(r["response_body_excerpt"]) <= 1000


def test_secret_straddling_error_cut_is_masked_and_suffix_kept() -> None:
    secret = "tok_sssss"
    r = _result(transport_error="ReadTimeout: " + "y" * 490 + secret, _error_suffix=" (timeout_ms=1500)")
    _redact_result_in_place(r, {secret})
    assert r["transport_error"].endswith(" (timeout_ms=1500)")
    assert "tok_" not in r["transport_error"]
    assert len(r["transport_error"]) <= 500


def test_secret_in_detail_is_masked_and_contains_observed_bounded() -> None:
    secret = "s3cr3t"
    r = _result(evaluations=[
        {"kind": "json_path_eq", "detail": {"observed": secret, "expected": "x"}},
        {"kind": "json_path_contains", "detail": {"observed": "a" * 2000 + secret}},
    ])
    _redact_result_in_place(r, {secret, "tok_sssss"})
    assert r["evaluations"][0]["detail"]["observed"] == "***"
    obs = r["evaluations"][1]["detail"]["observed"]
    assert len(obs) <= 1000 and secret not in obs
    assert r["url"] == "http://x/?k=***"


def test_bound_applies_without_secrets() -> None:
    r = _result(response_body_excerpt="z" * 5000, transport_error="E: " + "q" * 900)
    _redact_result_in_place(r, set())
    assert len(r["response_body_excerpt"]) == 1000 and len(r["transport_error"]) == 500


def test_json_type_names() -> None:
    f = engine_mod._json_type_name
    assert [f(None), f(True), f(1), f(1.5), f("s"), f([]), f({})] == [
        "null", "boolean", "number", "number", "string", "array", "object"
    ]


# ---------- strict configs (422 at write time) ----------

@pytest.mark.parametrize(
    "kind,config",
    [
        ("json_path_exists", {}),
        ("json_path_exists", {"path": "$"}),
        ("json_path_exists", {"path": "$.."}),
        ("json_path_not_exists", {"path": "$.a[]"}),
        ("json_path_exists", {"path": "$.a", "extra": 1}),
        ("json_path_contains", {"path": "$.a", "needle": ""}),
        ("json_path_contains", {"path": "$.a", "needle": 5}),
        ("json_path_cmp", {"path": "$.a", "op": "ge", "expected": 1}),
        ("json_path_cmp", {"path": "$.a", "op": "gt", "expected": "5"}),
        ("json_path_cmp", {"path": "$.a", "op": "gt", "expected": True}),
        ("json_path_cmp", {"path": "$.a", "op": "gt", "expected": float("nan")}),
        ("json_path_cmp", {"path": "$.a", "op": "gt", "expected": None}),
        ("json_path_cmp", {"path": 5, "op": "gt", "expected": 1}),
    ],
)
def test_malformed_configs_rejected(kind: str, config: dict) -> None:
    with pytest.raises(ValidationError):
        CreateEvaluationRequest(name="e", kind=kind, config=config)


@pytest.mark.parametrize(
    "kind,config",
    [
        ("json_path_exists", {"path": "$.a"}),
        ("json_path_not_exists", {"path": "$.items[-1].name"}),
        ("json_path_contains", {"path": "$.text", "needle": "{{X}}"}),
        ("json_path_cmp", {"path": "$.n", "op": "gte", "expected": 5}),
        ("json_path_cmp", {"path": "$.n", "op": "lt", "expected": 5.5}),
        ("json_path_eq", {"path": "$.roles[?(@.name=='default')].id", "expected": "x"}),
    ],
)
def test_wellformed_configs_accepted(kind: str, config: dict) -> None:
    ev = CreateEvaluationRequest(name="e", kind=kind, config=config)
    assert ev.config == config  # stored verbatim


# ---------- timeout_ms bounds, capture grammar, reserved names ----------

@pytest.mark.parametrize("value", [99, 60001, 0, -5, "500", 100.0])
def test_request_timeout_ms_out_of_range_or_non_int(value) -> None:
    with pytest.raises(ValidationError):
        CreateRequestRequest(name="r", method="GET", url="http://x", timeout_ms=value)
    with pytest.raises(ValidationError):
        FixtureRequest(name="r", method="GET", url="http://x", timeout_ms=value)


def test_request_timeout_ms_in_range_and_default() -> None:
    assert CreateRequestRequest(name="r", method="GET", url="http://x").timeout_ms is None
    assert CreateRequestRequest(name="r", method="GET", url="http://x", timeout_ms=15000).timeout_ms == 15000


def test_capture_grammar_enforced_and_run_id_reserved() -> None:
    CreateRequestRequest(name="r", method="GET", url="http://x", capture={"ID": "$.roles[?(@.name=='d')].id"})
    with pytest.raises(ValidationError):
        CreateRequestRequest(name="r", method="GET", url="http://x", capture={"ID": "$.a[]"})
    with pytest.raises(ValidationError):
        CreateRequestRequest(name="r", method="GET", url="http://x", capture={"RUN_ID": "$.id"})


def test_environment_run_id_reserved_everywhere() -> None:
    with pytest.raises(ValidationError):
        CreateEnvironmentRequest(name="e", variables={"RUN_ID": "x"})
    with pytest.raises(ValidationError):
        UpdateEnvironmentRequest(secrets={"RUN_ID": ""})
    with pytest.raises(ValidationError):
        FixtureEnvironment(name="e", variables={"RUN_ID": "x"})
    assert UpdateEnvironmentRequest(name="only").variables is None
    assert CreateEnvironmentRequest(name="e", variables={"BASE_URL": "http://x"}).variables


def test_checked_in_bundle_captures_match_grammar() -> None:
    import glob
    import json

    captures = [
        (r.get("capture") or {})
        for f in glob.glob("fixtures/*.json")
        for r in json.load(open(f)).get("requests", [])
    ]
    paths = [c for cap in captures for c in cap.values()]
    assert paths and all(is_valid_json_path(c) for c in paths)
    assert not [k for cap in captures for k in cap if k == "RUN_ID"]


# ---------- negated kinds: json_path_not_contains, json_path_neq (+ raw path="") ----------

def test_not_contains_pathed_hit_miss_missing_empty() -> None:
    # Needle ABSENT → passes; found=False.
    ok, d = _run("json_path_not_contains", {"path": "$.text", "needle": "zzz"})
    assert ok and d["found"] is False and d["missing"] is False and d["observed"] == "hello world"
    # Needle PRESENT → fails; found=True (membership exposed, not the verdict).
    ok, d = _run("json_path_not_contains", {"path": "$.text", "needle": "lo wo"})
    assert not ok and d["found"] is True
    # Missing path → False (platform parity: a missing path fails EVERY compare
    # operator; a blanket `not contains()` would flip this to a vacuous pass).
    ok, d = _run("json_path_not_contains", {"path": "$.absent", "needle": "x"})
    assert not ok and d["missing"] is True and d["observed_type"] is None
    # Empty needle → False ("" is in everything, so "" not in x is False).
    ok, d = _run("json_path_not_contains", {"path": "$.text", "needle": ""})
    assert not ok and d.get("empty_needle") is True


def test_not_contains_is_exact_str_parity_with_platform() -> None:
    # Negated mirror of the contains parity: verdict must equal
    # ``needle not in str(actual)`` for every type (str(), never json.dumps).
    cases = [
        ({"v": True}, "True"), ({"v": True}, "true"),
        ({"v": None}, "None"), ({"v": None}, "null"),
        ({"v": ["default", "care_team"]}, "'default'"),
        ({"v": ["default", "care_team"]}, '"default"'),
        ({"v": {"name": "default"}}, "'name'"),
    ]
    for body, needle in cases:
        ok, d = _run("json_path_not_contains", {"path": "$.v", "needle": needle}, body=body)
        expected = needle not in str(body["v"])
        assert ok is expected, f"{body['v']!r} not_contains {needle!r}: engine={ok} platform={expected}"
        assert d["observed"] == str(body["v"])


def test_contains_and_not_contains_raw_whole_body() -> None:
    # path="" is the raw whole-body match (platform path==""). Haystack is
    # str(body) — single-quoted repr, NOT json.dumps.
    body = {"role": "admin", "id": "u-1"}
    ok, d = _run("json_path_contains", {"path": "", "needle": "'role': 'admin'"}, body=body)
    assert ok and d["found"] and d["missing"] is False and d["observed"] == str(body)
    # The exact json.dumps-vs-str() divergence: the double-quoted form is NOT present.
    assert not _run("json_path_contains", {"path": "", "needle": '"role": "admin"'}, body=body)[0]
    # Raw not_contains: a token absent from the whole body → passes (the
    # scope-enforcement "field is absent" assertion).
    ok, d = _run("json_path_not_contains", {"path": "", "needle": "secret-leak"}, body=body)
    assert ok and d["found"] is False and d["observed"] == str(body)
    # ...and present → fails.
    assert not _run("json_path_not_contains", {"path": "", "needle": "u-1"}, body=body)[0]


def test_neq_hit_miss_missing_no_coercion() -> None:
    # Differs → passes.
    ok, d = _run("json_path_neq", {"path": "$.count", "expected": 4})
    assert ok and d["observed"] == 3
    # Equal → fails.
    ok, d = _run("json_path_neq", {"path": "$.count", "expected": 3})
    assert not ok
    # Missing path → False (platform parity; NOT a vacuous True).
    ok, d = _run("json_path_neq", {"path": "$.absent", "expected": 1})
    assert not ok and d["missing"] is True and d["observed"] is None
    # No coercion (plain !=): int 3 != str "3" → passes, exactly like platform's
    # `actual != expected` (contrast json_path_cmp, which floats both operands).
    ok, d = _run("json_path_neq", {"path": "$.count", "expected": "3"})
    assert ok
    # None expected against a present-null field → equal → fails.
    ok, d = _run("json_path_neq", {"path": "$.nothing", "expected": None})
    assert not ok


def test_render_config_negated_kinds_substitutes() -> None:
    # C3: without these fields in _RENDERED_CONFIG_FIELDS the {{VAR}} is compared
    # literally and the leak/scope assertion passes vacuously. Pin that they render.
    subs = {"CARE_UID": "u-9", "RUN_ID": "abc"}
    cfg, missed = _render_config("json_path_neq", {"path": "$.author_id", "expected": "{{CARE_UID}}"}, subs)
    assert cfg["expected"] == "u-9" and cfg["expected_raw"] == "{{CARE_UID}}" and missed == []
    cfg, missed = _render_config("json_path_not_contains", {"path": "$.messages", "needle": "brief {{RUN_ID}}"}, subs)
    assert cfg["needle"] == "brief abc" and cfg["needle_raw"] == "brief {{RUN_ID}}"
    # A non-scalar expected is NOT recursed (documented I-A limitation, inherited
    # from json_path_eq): the token survives. The Phase-B lint forbids this shape.
    cfg, _ = _render_config("json_path_neq", {"path": "$.x", "expected": {"id": "{{CARE_UID}}"}}, subs)
    assert cfg["expected"] == {"id": "{{CARE_UID}}"}

    # End-to-end leak detection: render then evaluate. The captured id actually
    # leaked (observed == resolved var) → neq must FAIL (catch the leak).
    rendered, _ = _render_config("json_path_neq", {"path": "$.author_id", "expected": "{{CARE_UID}}"}, subs)
    ok, _ = _run("json_path_neq", rendered, body={"author_id": "u-9"})
    assert ok is False


@pytest.mark.parametrize(
    "kind,config",
    [
        ("json_path_not_contains", {"path": "$.a", "needle": ""}),   # empty needle
        ("json_path_not_contains", {"path": "$.a", "needle": 5}),    # non-str needle
        ("json_path_not_contains", {"path": "$", "needle": "x"}),    # bare $ invalid
        ("json_path_not_contains", {"path": "$.", "needle": "x"}),   # $. invalid
        ("json_path_not_contains", {"path": "items", "needle": "x"}),  # bare (unprefixed) invalid
        ("json_path_not_contains", {"path": "$.a", "needle": "x", "extra": 1}),  # extra
        ("json_path_neq", {}),                                       # missing path
        ("json_path_neq", {"path": "$", "expected": "x"}),           # bare $ (strict)
        ("json_path_neq", {"path": "", "expected": "x"}),            # raw NOT allowed for neq
        ("json_path_neq", {"path": "$.a", "expected": "x", "extra": 1}),  # extra
        ("json_path_neq", {"path": "$.a", "expected": {"id": "{{X}}"}}),  # structured (fail-open)
        ("json_path_neq", {"path": "$.a", "expected": ["{{X}}"]}),        # structured list
    ],
)
def test_negated_kind_configs_rejected(kind: str, config: dict) -> None:
    with pytest.raises(ValidationError):
        CreateEvaluationRequest(name="e", kind=kind, config=config)


@pytest.mark.parametrize(
    "kind,config",
    [
        ("json_path_not_contains", {"path": "$.text", "needle": "x"}),   # pathed
        ("json_path_not_contains", {"path": "", "needle": "x"}),         # raw whole-body OK
        ("json_path_contains", {"path": "", "needle": "x"}),             # raw now accepted (was strict-rejected)
        ("json_path_contains", {"path": "$.items[0].id", "needle": "x"}),  # field-then-index still OK
        ("json_path_neq", {"path": "$.author_id", "expected": "x"}),
        ("json_path_neq", {"path": "$.comments[0].replies[0].body", "expected": None}),  # None expected OK
    ],
)
def test_negated_kind_configs_accepted(kind: str, config: dict) -> None:
    ev = CreateEvaluationRequest(name="e", kind=kind, config=config)
    assert ev.config == config  # stored verbatim


def test_collection_item_cap_admits_role_screenshots() -> None:
    # serve-role-screenshots is 170 items; the cap was raised 90->200 for it.
    # Guard against an accidental lowering below that floor.
    from api.runs._constants import COLLECTION_ITEM_CAP
    assert COLLECTION_ITEM_CAP >= 170
