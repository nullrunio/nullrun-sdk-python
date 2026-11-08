.PHONY: install test lint type-check coverage clean build publish-test publish smoke-test lint-supply-chain

# ── Setup ─────────────────────────────────────────────────────
install:
	pip install -e ".[dev]"
	pre-commit install

# Sprint 3.5 (B10): the ``protos`` target was removed. The
# ``./protos/nullrun/v1/track.proto`` directory was deleted
# when the gRPC transport was frozen in 0.3.1 (CHANGELOG
# 0.3.1:217-218). The target would fail on a current checkout
# with ``No such file or directory``. Re-introduce it ONLY
# when gRPC is unblocked (see README §"gRPC transport").

# Sprint 5: the ``run-example`` target was removed. The
# ``examples/`` directory was deleted along with the gRPC
# transport in 0.3.1, and the target referenced the now-missing
# ``examples/basic.py``. Local smoke-testing uses ``smoke-test``
# below instead.

# ── Tests ─────────────────────────────────────────────────────
test:
	pytest tests/ -v

test-watch:
	pytest tests/ -v --tb=short -f

# Sprint 5: align with CI (.github/workflows/ci.yml:82).
# ``coverage run -m pytest`` only traced the xdist coordinator,
# so every parallel run uploaded 0 hits. pytest-cov starts coverage
# in every worker and combines the data before producing the XML.
coverage:
	pytest tests/ --cov=src/nullrun --cov-branch --cov-report=xml:coverage.xml --cov-report=term
	@echo "XML report: coverage.xml"

# ── Code quality ──────────────────────────────────────────────
lint:
	ruff check src/ tests/
	ruff format --check src/ tests/

lint-fix:
	ruff check --fix src/ tests/
	ruff format src/ tests/

type-check:
	mypy src/nullrun --strict

check: lint lint-supply-chain type-check test

# ── Supply chain ──────────────────────────────────────────────
# SDK-3 (security audit 2026-11-06). Fails on a workflow that pins an
# action to a mutable ref, and on a dependency install that ignores
# uv.lock. CI runs the same script (.github/workflows/ci.yml, job
# ``supply-chain``) and the publish workflows run it inside the job that
# holds the PyPI Trusted Publishing token.
lint-supply-chain:
	python scripts/check_supply_chain.py

# ── Build & Publish ───────────────────────────────────────────
clean:
	rm -rf dist/ build/ *.egg-info htmlcov/ .coverage coverage.xml

# ``build`` and ``twine`` come from the ``publish`` dependency group in
# uv.lock, so the local publish path resolves the same versions as CI.
# ``--inexact`` keeps the dev packages that are already in the venv.
build: clean
	uv sync --frozen --only-group publish --inexact
	uv run --frozen --no-sync python -m build
	uv run --frozen --no-sync twine check dist/*

publish-test: build
	uv run --frozen --no-sync twine upload --repository testpypi dist/*

publish: build
	uv run --frozen --no-sync twine upload dist/*

# ── Dev helpers ───────────────────────────────────────────────
smoke-test: build
	pip install dist/*.whl --force-reinstall
	python -c "from nullrun import protect; print('OK')"