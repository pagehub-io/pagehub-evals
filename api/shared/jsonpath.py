"""JSONPath-lite grammar shared by request captures and path-bearing
evaluation kinds.

The tokenizer in ``api/runs/engine.py::_resolve_path`` accepts more than
this grammar (``$``, ``$.``, ``$..`` resolve to the whole body; ``$.a[]``
and ``$.a[0`` resolve to the sentinel). Validating against the grammar at
write time is what keeps a typo in a ``json_path_not_exists`` path from
becoming an assertion that can never fail.

Forms: ``$`` followed by one or more of ``.field``, ``[int]`` (negative
allowed), or the filter ``[?(@.key=='value')]`` / ``[?(@.key=="value")]``
(first object element whose ``key`` is a string equal to the literal).
Key segments exclude whitespace; quoted literals may contain it.
"""

from __future__ import annotations

import re

JSON_PATH_LITE_PATTERN = (
    r"^\$(?:\.[^.\[\]\s]+|\[-?\d+\]|"
    r"\[\?\(@\.[^.\[\]=\s]+==(?:'[^']*'|\"[^\"]*\")\)\])+$"
)
JSON_PATH_LITE_RE = re.compile(JSON_PATH_LITE_PATTERN)

# Names the run engine injects at run time; never accepted as an
# environment variable, secret, or capture name.
RESERVED_SUBSTITUTION_NAMES = frozenset({"RUN_ID"})


def is_valid_json_path(path: object) -> bool:
    return isinstance(path, str) and JSON_PATH_LITE_RE.fullmatch(path) is not None


def check_reserved_names(mapping: dict[str, str] | None, *, what: str) -> None:
    if not mapping:
        return
    for name in mapping:
        if name in RESERVED_SUBSTITUTION_NAMES:
            raise ValueError(
                f"{what} name {name!r} is reserved: the run engine sets it at run time"
            )
