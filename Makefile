.PHONY: install test lint type-check coverage clean build publish-test publish smoke-test

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

check: lint type-check test

# ── Build & Publish ───────────────────────────────────────────
clean:
	rm -rf dist/ build/ *.egg-info htmlcov/ .coverage coverage.xml

build: clean
	pip install build
	python -m build
	pip install twine
	twine check dist/*

publish-test: build
	twine upload --repository testpypi dist/*

publish: build
	twine upload dist/*

# ── Dev helpers ───────────────────────────────────────────────
smoke-test: build
	pip install dist/*.whl --force-reinstall
	python -c "from nullrun import protect; print('OK')"