# Build stage for Python SDK
FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy source first (needed for build with src layout)
COPY pyproject.toml ./
COPY src ./src
RUN pip install build && python -m build

# Runtime stage
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy builder output
COPY --from=builder /app/dist /app/dist
RUN pip install /app/dist/*.whl --force-reinstall

# Non-root user
RUN useradd -m -u 1000 nullrun
USER nullrun

# Install optional dependencies
# Sprint 1.3 (B9): the previous `nullrun-breaker[langgraph]` package
# does not exist in `pyproject.toml` (only `nullrun[langgraph]`).
# Installing the non-existent package would make `docker build` fail.
RUN pip install "nullrun[langgraph]"

ENTRYPOINT ["python", "-m", "nullrun.breaker"]
