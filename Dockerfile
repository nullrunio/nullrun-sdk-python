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

# Install optional dependencies (langgraph integration is the only
# one with a non-trivial extra deps tree at the moment). The
# `nullrun[langgraph]` extra is defined in pyproject.toml.
RUN pip install "nullrun[langgraph]"

# The SDK ships as a library — there is no `python -m nullrun.breaker`
# entry point. The default CMD is `python` so the user can wire
# their own agent. Override at run time:
#   docker run -it --rm nullrun-sdk python -c "from nullrun import protect; print('ok')"
CMD ["python"]
