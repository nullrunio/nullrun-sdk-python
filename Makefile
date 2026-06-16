.PHONY: install test lint type-check coverage clean build publish-test publish protos

# ── Setup ─────────────────────────────────────────────────────
install:
	pip install -e ".[dev]"
	pre-commit install

# ── Protobuf generation (uses ./protos/, no backend dependency) ─
protos:
	@echo "Generating Python gRPC stubs from ./protos/..."
	@mkdir -p src/nullrun/v1
	python -m grpc_tools.protoc \
		-I./protos \
		--python_out=./src/nullrun/v1 \
		--grpc_python_out=./src/nullrun/v1 \
		./protos/nullrun/v1/track.proto
	@touch src/nullrun/v1/__init__.py
	@echo "Done. Generated files: src/nullrun/v1/track_pb2.py, track_pb2_grpc.py"

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