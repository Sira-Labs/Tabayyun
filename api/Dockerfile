# syntax=docker/dockerfile:1.7
# Stage 1: build the Rust core wheel (manylinux-compatible via maturin in a Debian image).
FROM rust:1.85-bookworm AS core
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip \
 && pip3 install --break-system-packages "maturin>=1.7,<2" \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src/core
COPY core/Cargo.toml core/Cargo.lock core/rustfmt.toml ./
COPY core/tabayyun-core ./tabayyun-core
COPY core/tabayyun-cli ./tabayyun-cli
COPY core/tabayyun-py ./tabayyun-py
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/core/target \
    cd tabayyun-py && maturin build --release --out /wheels --compatibility linux

# Stage 2: Python API with the wheel installed (no compiler in the runtime image).
FROM python:3.12-slim-bookworm AS api
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_SYSTEM_PYTHON=1
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
COPY --from=core /wheels /wheels
COPY api/pyproject.toml api/uv.lock ./
COPY api/src ./src
# Install runtime deps from the lock (except the path dependency), then the built wheel.
RUN uv export --no-dev --no-emit-project --no-emit-package tabayyun-core --frozen -o /tmp/req.txt \
 && uv pip install --no-cache -r /tmp/req.txt /wheels/*.whl . \
 && rm -rf /wheels
RUN useradd --system --uid 10001 --create-home tabayyun \
 && mkdir -p /data/cache && chown -R tabayyun /data
USER tabayyun
ENV TABAYYUN_ENV=prod TABAYYUN_CACHE_DIR=/data/cache
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"
CMD ["uvicorn", "tabayyun.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
