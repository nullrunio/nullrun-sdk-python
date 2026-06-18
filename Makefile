.PHONY: install test lint type-check coverage clean build publish-test publish

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

# ── Tests ─────────────────────────────────────────────────────
test:
	pytest tests/ -v

test-watch:
	pytest tests/ -v --tb=short -f

coverage:
	coverage run -m pytest tests/
	coverage report
	coverage html
	@echo "HTML report: htmlcov/index.html"

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
	rm -rf dist/ build/ *.egg-info htmlcov/ .coverage

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
run-example:
	python examples/basic.py

smoke-test: build
	pip install dist/*.whl --force-reinstall
	python -c "from nullrun import protect; print('OK')"