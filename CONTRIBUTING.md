# Contributing

Thanks for adding to the NullRun Python SDK. This file covers the
mechanics of landing a change. For product context, see
[README.md](./README.md) and the [docs](https://docs.nullrun.io).

## Development setup

```bash
git clone https://github.com/nullrunio/nullrun-sdk-python
cd nullrun-sdk-python
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Python 3.10+ is required.

## Tests

```bash
pytest -q                       # full suite
pytest tests/test_v3_wire_contract.py::TestGateCache -q   # single file / class
```

Tests must pass before opening a PR. New public API requires tests —
no exceptions. Source-pin regression tests live alongside the code
they protect (see `tests/test_audit_p0_27_operation_id_hoist.py`
for the canonical pattern).

## Linting and types

```bash
ruff check src tests            # lint
ruff format src tests           # auto-format
mypy src/nullrun                # strict-ish type check
```

CI runs the same three steps plus pre-commit hooks (`trailing-whitespace`,
`end-of-file-fixer`, `check-yaml`, `check-toml`). Do not bypass with
`--no-verify`.

## Commit hygiene

We follow [Conventional Commits](https://www.conventionalcommits.org/).
Common prefixes used in this repo:

| Prefix     | Used for                                              |
| ---------- | ----------------------------------------------------- |
| `feat`     | New public API or behaviour                           |
| `fix`      | Correctness fixes (cite the defect id)                |
| `refactor` | Internal change with no observable behaviour shift    |
| `docs`     | README / CHANGELOG / docstring-only changes           |
| `test`     | New or rewritten tests                                |
| `chore`    | Release prep, dep bumps, CI plumbing                  |

Reference the defect id in the body when one exists
(`DEF-OPID-REUSE-HASH-MISMATCH`, `NR-007`, …).

## Pull requests

- One logical change per PR. Drive-bys bundled into unrelated PRs get
  rejected at review.
- PR description: what changed, why, how to verify, any wire-shape or
  ADR implications.
- Wire-contract changes (anything that touches `_V3_ERROR_CODE_MAP`,
  `transport.py`, the gate payload shape, or `runtime.check_workflow_budget`)
  require an ADR reference. Coordinate before opening.

## Issues

- Use the GitHub issue templates.
- Defects use the `DEF-*` prefix in the title. Probes / Q&A go to the
  relevant `qa/` subtree in `nullrun-examples` — not this repo.
- Security issues do **not** belong in public issues — see
[SECURITY.md](https://github.com/nullrunio/.github/blob/main/SECURITY.md).
