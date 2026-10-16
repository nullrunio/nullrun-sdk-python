"""``pip install nullrun[websocket]`` named an extra that did not exist.

## What was observed

QA (RUN_ID 20261015T1809, DEF-SDK-16) hit ``ImportError`` from
``WebSocketConnection.connect`` and followed the instruction the
message gave:

    websockets library not available. Install with:
    pip install nullrun[websocket]

pip rejected it — ``nullrun`` has no ``websocket`` extra.
``[project.optional-dependencies]`` declared exactly two: the
``opentelemetry`` pair and ``dev``. ``websockets`` appeared nowhere in
``pyproject.toml`` at all; it was present in ``uv.lock`` only as a
transitive dependency of ``langsmith``, itself pulled in by
``langchain-core``.

So the failure mode was worse than a missing feature: a user without
the library is told exactly how to install it, types it, and gets a
second error instead of the feature. The only way to reach the WS
transport was to know the real package name independently of the SDK's
own advice.

## What is pinned here

The general property, not the one string:

* every ``nullrun[<extra>]`` install hint that appears in ``src/``
  names an extra ``pyproject.toml`` actually declares, and
* the ``websocket`` extra resolves to a ``websockets`` specifier.

A new optional import added later with an uninstallable hint fails
here rather than in a user's terminal.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# `tomllib` is stdlib from Python 3.11; on 3.10 (the package's
# lower bound) the test must fall back to `tomli`, which the
# `dev` extra declares conditionally on `python_version < '3.11'`.
# A direct `import tomllib` fails at COLLECTION time on 3.10,
# before any test in the suite can run, so this fallback is the
# difference between "3.10 in CI green" and "3.10 errors the
# whole module at import".
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 in CI
    import tomli as tomllib

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# ``pip install nullrun[websocket]``, ``nullrun[dev,websocket]``, …
# Matched in source and docs alike; the assertion below is that every
# extra named in one of these has a declaration behind it.
_HINT_RE = re.compile(r"nullrun\[([a-z0-9_,.-]+)\]")


def _declared_extras() -> set[str]:
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    return set(pyproject["project"].get("optional-dependencies", {}))


def _hint_files() -> list[Path]:
    files = [
        path
        for path in _SRC.rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    return files


@pytest.mark.parametrize(
    "path", _hint_files(), ids=lambda p: p.name
)
def test_install_hints_name_declared_extras(path: Path) -> None:
    declared = _declared_extras()
    for match in _HINT_RE.finditer(path.read_text(encoding="utf-8")):
        for extra in match.group(1).split(","):
            assert extra.strip() in declared, (
                f"{path.name} tells the user to install "
                f"nullrun[{extra.strip()}], but pyproject.toml "
                f"declares no such extra (declared: {sorted(declared)})"
            )


def test_websocket_extra_is_declared() -> None:
    assert "websocket" in _declared_extras()


def test_websocket_extra_pulls_websockets() -> None:
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    specifiers = pyproject["project"]["optional-dependencies"]["websocket"]
    assert any(s.startswith("websockets") for s in specifiers), specifiers


def test_error_message_names_the_declared_extra() -> None:
    """The ImportError itself must point at the real extra.

    This is the assertion that would have caught DEF-SDK-16 directly:
    the message is the only place a user learns the install command, so
    if the extra is renamed or dropped the message has to fail here.
    """
    declared = _declared_extras()
    source = (_SRC / "nullrun" / "transport_websocket.py").read_text(
        encoding="utf-8"
    )
    hints = _HINT_RE.findall(source)
    assert hints, "the ImportError lost its install hint entirely"
    for group in hints:
        for extra in group.split(","):
            assert extra.strip() in declared


def test_floor_is_14_not_older() -> None:
    """``additional_headers`` is the 14.0+ spelling.

    ``transport_websocket`` calls
    ``websockets.connect(url, additional_headers=...)``. Releases before
    14.0 spelled that argument ``extra_headers``, so a floor below 14
    installs cleanly and then raises TypeError at connect time — later,
    in production traffic, rather than at the ImportError the user was
    told to fix.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    specifier = next(
        s
        for s in pyproject["project"]["optional-dependencies"]["websocket"]
        if s.startswith("websockets")
    )
    assert ">=14" in specifier, specifier


def test_dev_extra_also_carries_websockets() -> None:
    """CI installs ``.[dev]`` only.

    ``tests/test_ws_push.py`` imports ``websockets`` at module scope. It
    used to resolve through ``langchain-core`` -> ``langsmith`` ->
    ``websockets``, so the WS wire test's coverage depended on a
    dependency edge nobody here controls. Declared directly, that
    coverage survives an upstream prune.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    assert any(s.startswith("websockets") for s in dev), dev


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))