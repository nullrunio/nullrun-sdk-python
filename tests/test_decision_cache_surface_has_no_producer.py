"""The SDK has no decision cache, and its surface says it has one.

``DecisionSource.CACHED`` and ``ExecuteConfig.cache_ttl`` /
``cache_max_size`` sit in the public surface with no producer
anywhere in ``src/``. The SDK has no in-process gate cache
(NR-116 / ADR-180): every call is its own ``/gate`` call with its
own reservation. A vestigial member that reads as a live decision
cache is a trap -- a future "restore the cache" change would land
green, and a reader would assume a decision can be reused.

This file pins the ABSENCE of a producer. That is a textual
property of the tree, so it is checked with an AST walk rather than
a substring search -- a substring search would also match the
comment that documents the defect.

What this does NOT prove: that no future change adds a producer.
It proves that no producer exists now, and it fails the moment one
appears.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from nullrun.transport import DecisionSource, ExecuteConfig

_SRC_ROOT = pathlib.Path(__file__).resolve().parent.parent / "src" / "nullrun"


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in _SRC_ROOT.rglob("*.py"))


def _parsed() -> list[tuple[pathlib.Path, ast.Module]]:
    out: list[tuple[pathlib.Path, ast.Module]] = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out.append((path, tree))
    return out


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(_SRC_ROOT).as_posix()


def _set_elements(value: ast.expr) -> list[ast.expr] | None:
    """The elements of a set literal, whether it is written bare or
    wrapped in ``frozenset(...)`` / ``set(...)``."""
    if isinstance(value, ast.Set):
        return list(value.elts)
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        if value.func.id in {"frozenset", "set"} and value.args:
            first = value.args[0]
            if isinstance(first, ast.Set):
                return list(first.elts)
    return None


def _cached_occurrences() -> list[tuple[str, str]]:
    """Every ``"cached"`` string constant in the package, with the
    STRUCTURAL ROLE it plays.

    Two roles are surface and legal: the ``DecisionSource`` enum
    member, and the runtime's provenance allowlist. Every other
    appearance is reported as ``unclassified`` -- a literal nobody can
    account for is a producer until proven otherwise."""
    found: list[tuple[str, str]] = []
    for path, tree in _parsed():
        rel = _rel(path)
        roles: dict[int, str] = {}

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name != "DecisionSource":
                continue
            for stmt in node.body:
                if not isinstance(stmt, ast.Assign):
                    continue
                targets = [ast.unparse(t) for t in stmt.targets]
                if "CACHED" not in targets:
                    continue
                if isinstance(stmt.value, ast.Constant) and stmt.value.value == "cached":
                    roles[id(stmt.value)] = "enum_member"

        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [ast.unparse(t) for t in node.targets]
            if "_KNOWN_DECISION_SOURCES" not in targets:
                continue
            elements = _set_elements(node.value)
            if elements is None:
                continue
            for element in elements:
                if isinstance(element, ast.Constant) and element.value == "cached":
                    roles[id(element)] = "allowlist_member"

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "cached":
                found.append((rel, roles.get(id(node), "unclassified")))
    return found


class TestNoProducerForCached:
    """``DecisionSource.CACHED`` is never produced."""

    def test_the_member_is_never_referenced(self):
        """No attribute access ``DecisionSource.CACHED`` anywhere.

        The member exists for import compatibility. Referencing it
        would be a producer, or a comparison against a value that
        never arrives."""
        offenders: list[str] = []
        for path, tree in _parsed():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                if node.attr != "CACHED":
                    continue
                offenders.append(f"{_rel(path)}:{node.lineno}: {ast.unparse(node)}")
        assert not offenders, (
            "`DecisionSource.CACHED` is referenced in the package; the "
            "SDK has no decision cache, so nothing may read it:\n  "
            + "\n  ".join(offenders)
        )

    def test_the_literal_appears_only_as_surface(self):
        """The string ``"cached"`` appears exactly twice in the
        package, and both occurrences are surface:

        * the enum member in ``transport.py``;
        * the runtime's known-provenance allowlist, which accepts a
          body that CLAIMS the provenance -- it does not produce it.

        A third occurrence would be a producer, or a new surface that
        says the same thing a second way.

        The shape is pinned structurally, not by line number: a line
        number moves whenever anything above it is edited, and a pin
        that breaks on an unrelated edit is a broken pin."""
        kinds = _cached_occurrences()
        assert sorted(kinds) == [
            ("runtime.py", "allowlist_member"),
            ("transport.py", "enum_member"),
        ], (
            "The string 'cached' must appear exactly twice in the package "
            "-- the DecisionSource member and the runtime's provenance "
            "allowlist. A new occurrence is either a producer or a second "
            "spelling of the same vestigial shape: " + ", ".join(
                f"{f}:{k}" for f, k in kinds
            )
        )

    def test_the_allowlist_entry_is_a_set_member_not_a_producer(self):
        """The runtime's occurrence is an element of the
        ``_KNOWN_DECISION_SOURCES`` set. Pin the shape so the entry
        cannot quietly become a return value."""
        found = False
        for path, tree in _parsed():
            if _rel(path) != "runtime.py":
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    targets = [ast.unparse(t) for t in node.targets]
                    if "_KNOWN_DECISION_SOURCES" not in targets:
                        continue
                    elements = _set_elements(node.value)
                    assert elements is not None, (
                        "_KNOWN_DECISION_SOURCES must stay a set literal"
                    )
                    assert any(
                        isinstance(e, ast.Constant) and e.value == "cached"
                        for e in elements
                    ), "the allowlist no longer names 'cached'"
                    found = True
        assert found, "_KNOWN_DECISION_SOURCES was not found in runtime.py"


class TestNoReaderForTheCacheConfig:
    """``ExecuteConfig.cache_ttl`` / ``cache_max_size`` are never read."""

    @pytest.mark.parametrize("field", ["cache_ttl", "cache_max_size"])
    def test_the_field_is_never_read(self, field):
        """No attribute access anywhere in the package.

        The fields stay on the dataclass for import compatibility. A
        reader would be the first half of a decision cache."""
        offenders: list[str] = []
        for path, tree in _parsed():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == field:
                    offenders.append(f"{_rel(path)}:{node.lineno}: {ast.unparse(node)}")
        assert not offenders, (
            f"`ExecuteConfig.{field}` is read in the package; the SDK has "
            "no decision cache:\n  " + "\n  ".join(offenders)
        )

    def test_the_fields_still_exist_on_the_dataclass(self):
        """The pin above is about READERS. The surface itself is
        public and stays -- removing it would break importers for no
        safety gain."""
        assert ExecuteConfig().cache_ttl == 60.0
        assert ExecuteConfig().cache_max_size == 10000
        assert DecisionSource.CACHED == "cached"
