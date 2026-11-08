"""The supply-chain lint must fail on a bad workflow and pass on a good one.

SDK-3 (security audit 2026-11-06) closed the hole by pinning every action
to a commit SHA and installing from ``uv.lock``. A lint that nobody runs is
the same as no lint, so these tests exist to prove the lint is capable of
going red. A lint that only ever says OK proves nothing.

The same file also guards the pins that live outside a workflow:
``[build-system].requires`` and the ``publish`` dependency group. A build
backend is not part of ``uv.lock``, so ``uv lock --check`` cannot see it —
nothing but an explicit test keeps that pin honest.

The fixtures live in a temporary directory. The repository's own workflow
files are never modified to make a test pass.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_supply_chain.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def load_lint_module():
    """Import ``scripts/check_supply_chain.py`` by path."""
    spec = importlib.util.spec_from_file_location("check_supply_chain", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The module registers itself before it runs. Without this, the
    # ``@dataclass`` decorator cannot find the module that owns the class
    # and raises AttributeError.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lint = load_lint_module()


GOOD_WORKFLOW = """\
name: good
on:
  push:
    branches: [master]
jobs:
  test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
        with:
          fetch-depth: 0
      - uses: astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2 # v10.3.3
        with:
          version: "0.11.28"
          enable-cache: true
      - name: Install from the lock
        run: uv sync --frozen --extra dev
      - name: Run the tests
        run: uv run --frozen --extra dev pytest -n auto
"""


def write_workflow(directory: Path, body: str, name: str = "workflow.yml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


# The shape of the publish job: it holds the PyPI Trusted Publishing token.
TRUSTED_WORKFLOW = """\
name: trusted
on:
  push:
    tags: ["v*"]
jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
      - uses: astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2 # v10.3.3
        with:
          version: "0.11.28"
          enable-cache: true
      - name: Build
        run: uv run --frozen --only-group publish python -m build
"""


def rules_for(path: Path, use_yaml: bool | None = None) -> list[str]:
    violations = lint.find_violations([path], use_yaml=use_yaml)
    return sorted(violation.rule for violation in violations)


# ── The good case ───────────────────────────────────────────────────────────


def test_a_fully_pinned_workflow_passes(tmp_path: Path) -> None:
    path = write_workflow(tmp_path, GOOD_WORKFLOW)
    assert rules_for(path) == []


def test_the_repositories_own_workflows_pass() -> None:
    """The lint must be green on the repository it guards.

    This is the check that fails when a workflow is edited without a pin.
    """
    workflow_dir = REPO_ROOT / ".github" / "workflows"
    assert workflow_dir.is_dir()
    violations = lint.find_violations([workflow_dir])
    assert violations == [], "\n".join(violation.render() for violation in violations)


# ── The lint must be CALLED ───────────────────────────────────────────────────


def workflow_text(name: str) -> str:
    return (REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def workflow_document(name: str) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(workflow_text(name))


def test_the_lint_is_called_by_every_workflow() -> None:
    """A lint that exists but is never called catches nothing.

    Deleting the invocation from a workflow would leave the repository
    with a lint file that no runner executes. This test is what makes
    that deletion visible.
    """
    for name in ("ci.yml", "publish.yml", "publish-test.yml"):
        assert "scripts/check_supply_chain.py" in workflow_text(name), name


def test_the_lint_runs_in_the_publish_job_before_the_upload() -> None:
    """The publish job holds the PyPI Trusted Publishing token.

    The lint must run inside THAT job, and before the upload step. A
    lint in a different job only helps if that job gates the publish
    job; a lint inside the publish job helps even if it does not.
    """
    for name in ("publish.yml", "publish-test.yml"):
        job = workflow_document(name)["jobs"]["publish"]

        # The trust grant must survive the pin.
        assert job["permissions"].get("id-token") == "write", name

        lint_index = None
        upload_index = None
        for index, step in enumerate(job["steps"]):
            if "scripts/check_supply_chain.py" in step.get("run", ""):
                lint_index = index
            if str(step.get("uses", "")).startswith("pypa/gh-action-pypi-publish"):
                upload_index = index

        assert lint_index is not None, f"{name}: the publish job does not run the lint"
        assert upload_index is not None, f"{name}: the publish job does not upload"
        assert lint_index < upload_index, f"{name}: the lint runs after the upload"


# ── The bad cases ───────────────────────────────────────────────────────────


def test_a_tag_ref_is_rejected(tmp_path: Path) -> None:
    body = GOOD_WORKFLOW.replace(
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4",
        "actions/checkout@v4",
    )
    path = write_workflow(tmp_path, body)
    assert "action-ref-not-sha-pinned" in rules_for(path)


def test_a_branch_ref_is_rejected(tmp_path: Path) -> None:
    body = GOOD_WORKFLOW.replace(
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4",
        "actions/checkout@main",
    )
    path = write_workflow(tmp_path, body)
    assert "action-ref-not-sha-pinned" in rules_for(path)


def test_a_short_sha_is_rejected(tmp_path: Path) -> None:
    """A truncated SHA is not a SHA. It must not pass."""
    body = GOOD_WORKFLOW.replace(
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4",
        "actions/checkout@11d5960 # v4",
    )
    path = write_workflow(tmp_path, body)
    assert "action-ref-not-sha-pinned" in rules_for(path)


def test_an_unpinned_setup_uv_version_is_rejected(tmp_path: Path) -> None:
    body = GOOD_WORKFLOW.replace('version: "0.11.28"', 'version: "latest"')
    path = write_workflow(tmp_path, body)
    assert "action-version-not-pinned" in rules_for(path)


def test_a_ranged_pip_install_is_rejected(tmp_path: Path) -> None:
    body = GOOD_WORKFLOW.replace(
        "run: uv sync --frozen --extra dev",
        'run: pip install "pytest-xdist>=3.6"',
    )
    path = write_workflow(tmp_path, body)
    assert "dependency-not-exactly-pinned" in rules_for(path)


def test_a_bare_pip_install_is_rejected(tmp_path: Path) -> None:
    body = GOOD_WORKFLOW.replace(
        "run: uv sync --frozen --extra dev",
        "run: pip install --upgrade build",
    )
    path = write_workflow(tmp_path, body)
    assert "dependency-not-exactly-pinned" in rules_for(path)


def test_an_editable_install_is_rejected(tmp_path: Path) -> None:
    body = GOOD_WORKFLOW.replace(
        "run: uv sync --frozen --extra dev",
        'run: pip install -e ".[dev]"',
    )
    path = write_workflow(tmp_path, body)
    assert "dependency-not-exactly-pinned" in rules_for(path)


def test_an_uv_sync_without_frozen_is_rejected(tmp_path: Path) -> None:
    body = GOOD_WORKFLOW.replace(
        "run: uv sync --frozen --extra dev",
        "run: uv sync --extra dev",
    )
    path = write_workflow(tmp_path, body)
    assert "uv-sync-not-frozen" in rules_for(path)


def test_an_uv_sync_with_upgrade_is_rejected(tmp_path: Path) -> None:
    body = GOOD_WORKFLOW.replace(
        "run: uv sync --frozen --extra dev",
        "run: uv sync --frozen --upgrade",
    )
    path = write_workflow(tmp_path, body)
    assert "uv-sync-not-frozen" in rules_for(path)


def test_a_uv_pip_install_is_rejected(tmp_path: Path) -> None:
    body = GOOD_WORKFLOW.replace(
        "run: uv sync --frozen --extra dev",
        "run: uv pip install twine==7.0.0",
    )
    path = write_workflow(tmp_path, body)
    assert "uv-install-bypasses-lock" in rules_for(path)


def test_a_uv_run_without_frozen_is_rejected(tmp_path: Path) -> None:
    """``uv run`` syncs before it runs. Without ``--frozen`` it may
    re-resolve and rewrite the lock, so the reviewed lock stops being
    the contract."""
    body = GOOD_WORKFLOW.replace(
        "run: uv run --frozen --extra dev pytest -n auto",
        "run: uv run --extra dev pytest -n auto",
    )
    path = write_workflow(tmp_path, body)
    assert "uv-run-not-frozen" in rules_for(path)


def test_a_uv_run_with_locked_is_allowed(tmp_path: Path) -> None:
    """``--locked`` is the other accepted spelling. It fails when the
    lock is stale instead of rewriting it, which is the same promise
    ``--frozen`` makes."""
    body = GOOD_WORKFLOW.replace(
        "run: uv run --frozen --extra dev pytest -n auto",
        "run: uv run --locked --extra dev pytest -n auto",
    )
    path = write_workflow(tmp_path, body)
    assert rules_for(path) == []


def test_a_requirements_file_without_hashes_is_rejected(tmp_path: Path) -> None:
    body = GOOD_WORKFLOW.replace(
        "run: uv sync --frozen --extra dev",
        "run: pip install -r requirements.txt",
    )
    path = write_workflow(tmp_path, body)
    assert "requirements-file-without-hashes" in rules_for(path)


def test_an_exact_pip_pin_is_allowed(tmp_path: Path) -> None:
    """An exact ``==`` pin is not a ranged install, so it must pass.

    The audit asks for an exact pin OR a lock-based install. This rule does
    not force every workflow onto uv; it forbids the unpinned form.
    """
    body = GOOD_WORKFLOW.replace(
        "run: uv sync --frozen --extra dev",
        "run: pip install twine==7.0.0",
    )
    path = write_workflow(tmp_path, body)
    assert rules_for(path) == []


def test_a_lock_only_publish_job_passes(tmp_path: Path) -> None:
    """The publish job installs from the lock. It must be clean."""
    path = write_workflow(tmp_path, TRUSTED_WORKFLOW)
    assert rules_for(path) == []


def test_a_pip_install_in_a_trusted_job_is_rejected(tmp_path: Path) -> None:
    """Inside a job that holds the PyPI OIDC token, pip installs nothing.

    An exact ``==`` pin is not enough there: the job resolves outside
    uv.lock, which is the hole the audit found. The lock-based install
    is the only allowed form.
    """
    body = TRUSTED_WORKFLOW.replace(
        "run: uv run --frozen --only-group publish python -m build",
        "run: pip install twine==7.0.0",
    )
    path = write_workflow(tmp_path, body)
    assert "pip-install-in-trusted-job" in rules_for(path)


def test_the_trusted_job_rule_holds_without_pyyaml(tmp_path: Path) -> None:
    """The rule must survive the text scanner.

    A CI runner without PyYAML must not enforce less than a runner with
    it, or the rule is a rule that only some runs apply.
    """
    body = TRUSTED_WORKFLOW.replace(
        "run: uv run --frozen --only-group publish python -m build",
        "run: pip install twine==7.0.0",
    )
    path = write_workflow(tmp_path, body)
    assert "pip-install-in-trusted-job" in rules_for(path, use_yaml=False)


# ── The text fallback must agree with the YAML path ─────────────────────────


def test_the_text_scanner_agrees_with_the_yaml_scanner(tmp_path: Path) -> None:
    """The fallback must find the same problems, or it is a hole.

    A CI runner without PyYAML must not silently check less than a runner
    with it.
    """
    body = GOOD_WORKFLOW.replace(
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4",
        "actions/checkout@v4",
    ).replace(
        "run: uv sync --frozen --extra dev",
        "run: pip install --upgrade build",
    )
    path = write_workflow(tmp_path, body)
    with_yaml = rules_for(path, use_yaml=True)
    without_yaml = rules_for(path, use_yaml=False)
    assert with_yaml == without_yaml
    assert "action-ref-not-sha-pinned" in without_yaml
    assert "dependency-not-exactly-pinned" in without_yaml


# ── The pins that live outside a workflow ───────────────────────────────────
#
# A build backend is NOT part of uv.lock's resolution: uv reads
# ``[build-system].requires`` when it builds, and the result never enters
# the lock file. So ``uv lock --check`` cannot see it, and the workflow
# lint cannot see it either. One line of pyproject.toml is all it takes to
# bring the hole back:
#
#     requires = ["hatchling"]
#
# These tests are the guard for that line.


EXACT_PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[^\s,;]+$")

# The shape the repository had before SDK-3. A fixture, not a copy of the
# real file: the check must fail on the FORM, not on a stored copy of the
# answer.
PRE_FIX_PYPROJECT = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[dependency-groups]
publish = [
    "build",
    "twine==7.0.0",
]
"""


def load_toml(path: Path) -> dict:
    """Read a TOML file with the standard library.

    ``tomllib`` is stdlib from 3.11. On 3.10 the test falls back to
    ``tomli``, which the dev extra already declares for exactly this.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib.loads(path.read_text(encoding="utf-8"))


def pin_problems(pyproject: dict) -> list[str]:
    """Return every pin problem in ``pyproject``. Empty means clean."""
    problems: list[str] = []

    build_system = pyproject.get("build-system", {})
    requires = build_system.get("requires", [])
    if not requires:
        problems.append("build-system.requires is empty: no build backend is declared")

    names = []
    for entry in requires:
        if not EXACT_PIN.match(entry):
            problems.append(f"build-system.requires entry is not an exact == pin: {entry}")
        names.append(entry.split("==", 1)[0])

    backend = build_system.get("build-backend", "")
    if backend.startswith("hatchling") and "hatchling" not in names:
        problems.append(
            f"build-system.requires does not name hatchling while the build backend is {backend}"
        )

    groups = pyproject.get("dependency-groups", {})
    if "publish" not in groups:
        problems.append("dependency-groups.publish is missing")
    for entry in groups.get("publish", []):
        if not EXACT_PIN.match(entry):
            problems.append(f"dependency-groups.publish entry is not an exact == pin: {entry}")

    return problems


def test_the_build_backend_is_exactly_pinned() -> None:
    """``[build-system].requires`` must carry exact pins.

    This is the pin the publish job resolves inside the job that holds the
    PyPI Trusted Publishing token.
    """
    problems = pin_problems(load_toml(PYPROJECT))
    assert problems == [], "\n".join(problems)


def test_the_publish_group_is_exactly_pinned() -> None:
    """The publish tooling must come from the lock, not from a range."""
    groups = load_toml(PYPROJECT).get("dependency-groups", {})
    assert "publish" in groups, "the publish dependency group is missing"
    for entry in groups["publish"]:
        assert EXACT_PIN.match(entry), f"not an exact == pin: {entry}"


def test_the_pre_fix_pyproject_fails_the_pin_check(tmp_path: Path) -> None:
    """The RED proof.

    The form the repository had before SDK-3 — an unpinned build backend
    and an unpinned publish entry — must fail. Without this, the check
    above might be a check that passes for any input.
    """
    path = tmp_path / "pyproject.toml"
    path.write_text(PRE_FIX_PYPROJECT, encoding="utf-8")

    problems = pin_problems(load_toml(path))
    assert problems, "the pre-fix pyproject must not pass"
    assert any("hatchling" in problem for problem in problems)
    assert any("build" in problem for problem in problems)


# ── The script as a process ─────────────────────────────────────────────────


def run_script(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def test_the_script_exits_zero_on_the_repository(tmp_path: Path) -> None:
    result = run_script()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "supply-chain: OK" in result.stdout


def test_the_script_exits_nonzero_on_a_broken_workflow(tmp_path: Path) -> None:
    """The proof that the lint can go red.

    The broken file lives in a temporary directory. The repository's own
    workflows are never touched.
    """
    body = GOOD_WORKFLOW.replace(
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4",
        "actions/checkout@v4",
    )
    workflow = write_workflow(tmp_path / "workflows", body)
    assert workflow.exists()

    result = run_script(str(workflow))
    assert result.returncode == 1
    assert "action-ref-not-sha-pinned" in result.stdout
    assert "actions/checkout@v4" in result.stdout


def test_the_lint_catches_a_mutable_ref_and_a_ranged_install(tmp_path: Path) -> None:
    """Both holes of SDK-3 in one file: a tag ref and a ranged install.

    This is the shape the audit described — a workflow that pins nothing
    and resolves its dependencies at run time. The lint must name both.
    """
    body = GOOD_WORKFLOW.replace(
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4",
        "actions/checkout@v4",
    ).replace(
        "run: uv sync --frozen --extra dev",
        'run: pip install -e ".[dev]" "pytest-xdist>=3.6"',
    )
    workflow = write_workflow(tmp_path / "workflows", body)

    result = run_script(str(workflow))
    assert result.returncode == 1
    assert "action-ref-not-sha-pinned" in result.stdout
    assert "dependency-not-exactly-pinned" in result.stdout


def test_the_script_self_test_passes() -> None:
    """``--self-test`` plants a broken workflow and asserts the lint is
    red on it. If the lint could not go red, the self-test fails."""
    result = run_script("--self-test")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "self-test OK" in result.stdout


def test_the_script_resolves_paths_relative_to_itself(tmp_path: Path) -> None:
    """Running from another directory must check the same files.

    The script resolves the repository from its own location, never from
    the current directory. A CI job can ``cd`` anywhere.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=elsewhere,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "supply-chain: OK" in result.stdout


def test_the_script_reports_a_missing_path(tmp_path: Path) -> None:
    result = run_script(str(tmp_path / "nope.yml"))
    assert result.returncode == 2
    assert "no such path" in result.stderr


@pytest.mark.parametrize("body", [GOOD_WORKFLOW])
def test_the_fixture_is_a_valid_workflow(body: str) -> None:
    """The good fixture must parse. A fixture that does not parse would make
    every test above pass for the wrong reason."""
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(body)
    assert document["jobs"]["test"]["steps"]
