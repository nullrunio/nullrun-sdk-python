#!/usr/bin/env python3
"""Fail when a workflow pins an action to a mutable ref, or installs a
dependency outside the lock file.

SDK-3 (security audit 2026-11-06) found three holes in this repository's CI:

1. Every ``uses:`` pointed at a tag or a branch. A tag or a branch is a
   mutable name: whoever controls the action repository can move it, and the
   workflow then runs code that was never reviewed here. The fix is to pin the
   40-hex commit SHA and keep the tag in a trailing comment.
2. No workflow consumed ``uv.lock``. Each job ran ``pip install -e ".[dev]"``,
   so the resolved versions were chosen at run time, not at commit time.
3. The publish job ran ``pip install --upgrade build`` and ``pip install
   twine`` inside the job that holds the PyPI Trusted Publishing token.

This script is the check that keeps those holes closed. It exits non-zero and
prints one line per violation.

Rules
-----

``action-ref-not-sha-pinned``
    A ``uses:`` value must be ``owner/repo@<40 lowercase hex>``. A local
    action (``./path``) is allowed: it lives in this repository, so the
    commit already pins it. A ``docker://`` image must be pinned by digest.

``action-version-not-pinned``
    ``astral-sh/setup-uv`` must name an exact ``version``. Without it the
    action downloads whatever ``latest`` resolves to.

``dependency-not-exactly-pinned``
    A ``pip install`` requirement must be an exact ``==`` pin. A range
    (``>=``), a bare name, or an editable install of this project all resolve
    at run time.

``requirements-file-without-hashes``
    ``pip install -r FILE`` must carry ``--require-hashes``.

``uv-sync-not-frozen``
    A ``uv sync`` must carry ``--frozen`` or ``--locked``, and must not carry
    ``--upgrade``. Without one of those the job can rewrite the lock file.

``uv-run-not-frozen``
    A ``uv run`` must carry ``--frozen`` or ``--locked``. Without one of those
    the command can re-resolve and rewrite the lock file before it runs.

``uv-install-bypasses-lock``
    ``uv pip install``, ``uv add``, ``uv remove``, ``uvx`` and ``uv tool run``
    install a package outside the lock file.

``pip-install-in-trusted-job``
    A workflow that holds an OIDC token (``id-token: write``) must not install
    packages with ``pip`` at all, not even with an exact ``==`` pin. Inside
    that job a pip install resolves outside ``uv.lock``, which is the hole
    the audit found. The job installs from the lock with ``uv sync
    --frozen`` instead.

Usage
-----

``python scripts/check_supply_chain.py`` checks ``.github/workflows`` in this
repository. Pass file or directory arguments to check something else. The
paths resolve relative to this script, never to the current directory.

``python scripts/check_supply_chain.py --self-test`` plants a broken workflow
in a temporary directory and asserts the lint goes red on it. A lint that has
only ever been green on a clean tree may be a lint that never fails.

The script needs no third-party package. PyYAML is used when it is
importable; otherwise the same rules run over a text scan of the file.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

WORKFLOW_SUFFIXES = (".yml", ".yaml")

# ── Patterns ────────────────────────────────────────────────────────────────

SHA40 = re.compile(r"^[0-9a-f]{40}$")
PINNED_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@([0-9a-f]{40})$")
DOCKER_DIGEST = re.compile(r"^docker://[^\s@]+@sha256:[0-9a-f]{64}$")
LOCAL_ACTION = re.compile(r"^\./")
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")

# ``name==1.2.3`` or ``name[extra]==1.2.3``. A range, a bare name, a URL and
# an editable path all fail this.
EXACT_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]+\])?==[^\s,;]+$")

USES_LINE = re.compile(r"^(?P<indent>\s*)(?:-\s+)?uses:\s*(?P<value>\S+)")
RUN_LINE = re.compile(r"^(?P<indent>\s*)(?:-\s+)?run:\s*(?P<rest>.*)$")
BLOCK_SCALAR = re.compile(r"^[|>][-+]?\d*$")

# ``pip install`` and its variants. ``uv pip install`` is NOT here: it
# bypasses uv.lock even when the version is exact, so it has its own rule.
PIP_INSTALL = re.compile(
    r"^(?:(?:python[0-9.]*|py)\s+-m\s+pip|pip[0-9.]*)\s+install\b(?P<rest>.*)$"
)
UV_SYNC = re.compile(r"^uv\s+sync\b(?P<rest>.*)$")
UV_RUN = re.compile(r"^uv\s+run\b(?P<rest>.*)$")
UV_PIP_INSTALL = re.compile(r"^uv\s+pip\s+install\b(?P<rest>.*)$")
UV_MUTATE = re.compile(r"^uv\s+(?:add|remove)\b")
UV_TOOL = re.compile(r"^(?:uvx|uv\s+tool\s+run)\b(?P<rest>.*)$")

SHELL_SEPARATORS = re.compile(r"&&|\|\||[;|]")

# A workflow that asks for an OIDC token. Inside that workflow a pip
# install is the hole the audit found, even with an exact pin.
OAUTH_TRUST = re.compile(r"id-token:\s*write")

# A workflow with the two holes the audit found: a mutable action ref and a
# dependency that resolves at run time. The self-test plants this and
# asserts the lint goes red on it.
BROKEN_WORKFLOW = """\
name: broken
on:
  push:
    branches: [master]
jobs:
  test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - name: Install dependencies
        run: pip install -e ".[dev]" "pytest-xdist>=3.6"
"""

EXPECTED_SELF_TEST_RULES = {
    "action-ref-not-sha-pinned",
    "dependency-not-exactly-pinned",
}


@dataclass(frozen=True)
class Violation:
    """One rule failure at one place in one file."""

    path: str
    line: int
    rule: str
    subject: str
    message: str

    def render(self) -> str:
        where = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"{where}: {self.rule}: {self.message} (offending: {self.subject})"


# ── Reading the workflow ────────────────────────────────────────────────────


def workflow_files(paths: list[Path]) -> list[Path]:
    """Expand the arguments into the list of workflow files to check."""
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(
                sorted(
                    child
                    for child in path.rglob("*")
                    if child.is_file() and child.suffix in WORKFLOW_SUFFIXES
                )
            )
        elif path.is_file():
            found.append(path)
        else:
            raise FileNotFoundError(f"no such path: {path}")
    return found


def yaml_available() -> bool:
    try:
        import yaml  # noqa: F401
    except Exception:
        return False
    return True


def structural_content(text: str) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Return the ``uses`` values and the ``run`` blocks of a workflow.

    The parse goes through ``yaml.compose`` rather than ``yaml.safe_load``
    because the composed node carries the line of every key and value. The
    rules report a file position, so the position must be the real one and
    not "the first line that mentions this string" — two steps in one file
    can use the same action, and each is a separate finding.
    """
    try:
        import yaml
    except Exception:
        return [], []

    try:
        document = yaml.compose(text)
    except Exception:
        return [], []

    uses: list[tuple[str, int]] = []
    runs: list[tuple[str, int]] = []

    def walk(node: object) -> None:
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                if not isinstance(key_node, yaml.ScalarNode):
                    continue
                if key_node.value == "uses" and isinstance(value_node, yaml.ScalarNode):
                    uses.append((value_node.value, value_node.start_mark.line + 1))
                elif key_node.value == "run" and isinstance(value_node, yaml.ScalarNode):
                    # A block scalar (``run: |``) starts on the line AFTER
                    # the key. A plain scalar (``run: pytest``) starts on the
                    # key's own line.
                    block = value_node.style in ("|", ">")
                    runs.append(
                        (value_node.value, value_node.start_mark.line + 1 + (1 if block else 0))
                    )
                else:
                    walk(value_node)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item)

    walk(document)
    return uses, runs


def text_content(text: str) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Return the ``uses`` values and the ``run`` blocks of a workflow.

    This is the fallback for a runner without PyYAML. It reads the same
    surfaces a workflow file actually has: a ``uses:`` key and a ``run:``
    key with either an inline value or a block scalar.
    """
    lines = text.splitlines()
    uses: list[tuple[str, int]] = []
    runs: list[tuple[str, int]] = []

    index = 0
    while index < len(lines):
        line = lines[index]

        match = USES_LINE.match(line)
        if match:
            value = match.group("value").split("#", 1)[0].strip()
            if value:
                uses.append((value, index + 1))

        run_match = RUN_LINE.match(line)
        if run_match:
            rest = run_match.group("rest").strip()
            if BLOCK_SCALAR.match(rest):
                base_indent = len(run_match.group("indent"))
                block: list[str] = []
                cursor = index + 1
                while cursor < len(lines):
                    candidate = lines[cursor]
                    if not candidate.strip():
                        block.append("")
                        cursor += 1
                        continue
                    if len(candidate) - len(candidate.lstrip()) <= base_indent:
                        break
                    block.append(candidate)
                    cursor += 1
                runs.append(("\n".join(block), index + 2))
                index = cursor
                continue
            if rest:
                runs.append((rest, index + 1))

        index += 1

    return uses, runs


# ── The rules ───────────────────────────────────────────────────────────────


def check_uses(value: str, path: str, line: int) -> list[Violation]:
    subject = value.strip()
    if LOCAL_ACTION.match(subject):
        return []
    if subject.startswith("docker://"):
        if DOCKER_DIGEST.match(subject):
            return []
        return [
            Violation(
                path=path,
                line=line,
                rule="action-ref-not-sha-pinned",
                subject=subject,
                message=(
                    "a container image must be pinned by digest (docker://image@sha256:<64 hex>)"
                ),
            )
        ]
    if PINNED_ACTION.match(subject):
        return []
    return [
        Violation(
            path=path,
            line=line,
            rule="action-ref-not-sha-pinned",
            subject=subject,
            message=("an action must be pinned to a 40-hex commit SHA (owner/repo@<sha> # <tag>)"),
        )
    ]


def check_setup_uv_version(text: str, path: str) -> list[Violation]:
    """``astral-sh/setup-uv`` must name an exact version."""
    violations: list[Violation] = []
    lines = text.splitlines()

    for number, line in enumerate(lines, start=1):
        match = USES_LINE.match(line)
        if not match:
            continue
        value = match.group("value").split("#", 1)[0].strip()
        if not value.startswith("astral-sh/setup-uv@"):
            continue

        version = ""
        version_line = 0
        cursor = number
        while cursor < len(lines):
            candidate = lines[cursor]
            if candidate.strip() and re.match(r"^\s*(?:-\s+)?uses:", candidate):
                break
            version_match = re.match(r"^\s*version:\s*['\"]?([^'\"\s]+)", candidate)
            if version_match:
                version = version_match.group(1)
                version_line = cursor + 1
                break
            cursor += 1

        if not version:
            violations.append(
                Violation(
                    path=path,
                    line=number,
                    rule="action-version-not-pinned",
                    subject=value,
                    message=(
                        'astral-sh/setup-uv must set an exact version (with: version: "X.Y.Z")'
                    ),
                )
            )
        elif not EXACT_VERSION.match(version):
            violations.append(
                Violation(
                    path=path,
                    line=version_line,
                    rule="action-version-not-pinned",
                    subject=version,
                    message=(
                        'astral-sh/setup-uv must set an exact version (with: version: "X.Y.Z")'
                    ),
                )
            )

    return violations


def split_shell(line: str) -> list[str]:
    """Split one shell line into the commands it runs."""
    parts = SHELL_SEPARATORS.split(line)
    return [part.strip() for part in parts if part.strip()]


def check_pip_install(command: str, path: str, line: int) -> list[Violation]:
    match = PIP_INSTALL.match(command)
    if not match:
        return []

    rest = match.group("rest").strip()
    try:
        tokens = shlex.split(rest)
    except ValueError:
        tokens = rest.split()

    flags = [token for token in tokens if token.startswith("-")]
    requirements: list[str] = []
    editable = False
    requirement_file = False

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-e", "--editable"):
            editable = True
        elif token in ("-r", "--requirement"):
            requirement_file = True
            index += 1
            if index < len(tokens):
                requirements.append(tokens[index])
            index += 1
            continue
        elif token.startswith("-"):
            pass
        else:
            requirements.append(token)
        index += 1

    violations: list[Violation] = []

    if requirement_file and "--require-hashes" not in flags:
        violations.append(
            Violation(
                path=path,
                line=line,
                rule="requirements-file-without-hashes",
                subject=command,
                message="pip install -r must carry --require-hashes",
            )
        )

    if editable:
        violations.append(
            Violation(
                path=path,
                line=line,
                rule="dependency-not-exactly-pinned",
                subject=command,
                message=(
                    "an editable install of this project resolves its dependencies at run time"
                ),
            )
        )
        return violations

    for requirement in requirements:
        if requirement_file and requirement.endswith((".txt", ".in")):
            continue
        if EXACT_REQUIREMENT.match(requirement):
            continue
        violations.append(
            Violation(
                path=path,
                line=line,
                rule="dependency-not-exactly-pinned",
                subject=requirement,
                message=(
                    "a dependency must be an exact == pin; use "
                    "uv sync --frozen to install from uv.lock"
                ),
            )
        )

    return violations


def check_uv_sync(command: str, path: str, line: int) -> list[Violation]:
    match = UV_SYNC.match(command)
    if not match:
        return []

    try:
        tokens = shlex.split(match.group("rest"))
    except ValueError:
        tokens = match.group("rest").split()

    violations: list[Violation] = []
    if "--frozen" not in tokens and "--locked" not in tokens:
        violations.append(
            Violation(
                path=path,
                line=line,
                rule="uv-sync-not-frozen",
                subject=command,
                message="uv sync must carry --frozen or --locked",
            )
        )
    if "--upgrade" in tokens or "--upgrade-package" in tokens:
        violations.append(
            Violation(
                path=path,
                line=line,
                rule="uv-sync-not-frozen",
                subject=command,
                message="uv sync must not carry --upgrade in CI",
            )
        )
    return violations


def check_uv_run(command: str, path: str, line: int) -> list[Violation]:
    """``uv run`` must not be allowed to re-resolve the lock.

    ``uv run`` syncs the project environment before it runs the command.
    Without ``--frozen`` or ``--locked`` that sync may re-resolve and
    rewrite ``uv.lock``, which turns a reviewed lock into a suggestion.
    """
    match = UV_RUN.match(command)
    if not match:
        return []

    try:
        tokens = shlex.split(match.group("rest"))
    except ValueError:
        tokens = match.group("rest").split()

    if "--frozen" in tokens or "--locked" in tokens:
        return []

    return [
        Violation(
            path=path,
            line=line,
            rule="uv-run-not-frozen",
            subject=command,
            message="uv run must carry --frozen or --locked",
        )
    ]


def check_no_pip_in_trusted_workflow(
    text: str, runs: list[tuple[str, int]], path: str
) -> list[Violation]:
    """No pip install in a workflow that holds an OIDC token.

    The rule is per workflow, not per job: a file that asks for
    ``id-token: write`` anywhere must not install packages with pip
    anywhere. The scan runs over the raw text, so the rule holds in the
    text scanner too and not only when PyYAML is importable.
    """
    if not OAUTH_TRUST.search(text):
        return []

    violations: list[Violation] = []
    for block, first_line in runs:
        for offset, raw in enumerate(block.splitlines()):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for command in split_shell(stripped):
                if PIP_INSTALL.match(command):
                    violations.append(
                        Violation(
                            path=path,
                            line=first_line + offset,
                            rule="pip-install-in-trusted-job",
                            subject=command,
                            message=(
                                "this workflow holds an OIDC token "
                                "(id-token: write); install from uv.lock "
                                "with uv sync --frozen, never with pip"
                            ),
                        )
                    )
    return violations


def check_run_block(block: str, path: str, first_line: int) -> list[Violation]:
    """Apply the install rules to every command in one ``run:`` block."""
    violations: list[Violation] = []
    for offset, raw in enumerate(block.splitlines()):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for command in split_shell(stripped):
            if PIP_INSTALL.match(command):
                violations.extend(check_pip_install(command, path, first_line + offset))
            elif UV_SYNC.match(command):
                violations.extend(check_uv_sync(command, path, first_line + offset))
            elif UV_RUN.match(command):
                violations.extend(check_uv_run(command, path, first_line + offset))
            elif (
                UV_PIP_INSTALL.match(command) or UV_MUTATE.match(command) or UV_TOOL.match(command)
            ):
                violations.append(
                    Violation(
                        path=path,
                        line=first_line + offset,
                        rule="uv-install-bypasses-lock",
                        subject=command,
                        message=(
                            "this command installs a package outside uv.lock; use uv sync --frozen"
                        ),
                    )
                )
    return violations


# ── The entry points ────────────────────────────────────────────────────────


def find_violations(paths: list[Path], use_yaml: bool | None = None) -> list[Violation]:
    """Check every workflow under ``paths`` and return the violations."""
    if use_yaml is None:
        use_yaml = yaml_available()

    violations: list[Violation] = []
    for workflow in workflow_files(paths):
        text = workflow.read_text(encoding="utf-8")
        display = workflow.as_posix()

        if use_yaml:
            uses, runs = structural_content(text)
        else:
            uses, runs = text_content(text)

        for value, line in uses:
            violations.extend(check_uses(value, display, line))

        violations.extend(check_setup_uv_version(text, display))

        violations.extend(check_no_pip_in_trusted_workflow(text, runs, display))

        for block, first_line in runs:
            violations.extend(check_run_block(block, display, first_line))

    return violations


def self_test() -> int:
    """Prove the lint goes red.

    The lint is written to fail, but a rule that never fires is a rule
    that is not there. This plants a workflow with a mutable action ref
    and a run-time dependency install, then checks that BOTH scanners
    (the YAML one and the text one) report it.
    """
    with tempfile.TemporaryDirectory() as directory:
        workflow = Path(directory) / "broken.yml"
        workflow.write_text(BROKEN_WORKFLOW, encoding="utf-8")

        for use_yaml in (True, False):
            violations = find_violations([workflow], use_yaml=use_yaml)
            found = {violation.rule for violation in violations}
            missing = EXPECTED_SELF_TEST_RULES - found
            if missing:
                scanner = "yaml" if use_yaml else "text"
                print(
                    f"supply-chain: self-test FAILED ({scanner} scanner did not "
                    f"report {sorted(missing)})",
                    file=sys.stderr,
                )
                return 1

    print(
        "supply-chain: self-test OK (both scanners catch a mutable action ref "
        "and a run-time dependency install)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail on a mutable action ref or an unpinned dependency install."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help=(
            "workflow files or directories to check. "
            f"Defaults to {DEFAULT_WORKFLOW_DIR.relative_to(REPO_ROOT).as_posix()}."
        ),
    )
    parser.add_argument(
        "--no-yaml",
        action="store_true",
        help="use the text scanner even when PyYAML is importable",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="plant a broken workflow and assert the lint goes red on it",
    )
    arguments = parser.parse_args(argv)

    if arguments.self_test:
        return self_test()

    paths = arguments.paths or [DEFAULT_WORKFLOW_DIR]

    try:
        violations = find_violations(paths, use_yaml=not arguments.no_yaml)
    except FileNotFoundError as error:
        print(f"supply-chain: {error}", file=sys.stderr)
        return 2

    if violations:
        for violation in violations:
            print(violation.render())
        print(
            f"supply-chain: {len(violations)} violation(s) in "
            f"{len({violation.path for violation in violations})} file(s)",
            file=sys.stderr,
        )
        return 1

    print("supply-chain: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
