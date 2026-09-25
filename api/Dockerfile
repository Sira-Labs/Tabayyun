# syntax=docker/dockerfile:1.7
# Stage 1: build the Rust core wheel (manylinux-compatible via maturin in a Debian image).
FROM rust:1.98-bookworm AS core
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
FROM python:3.14-slim-bookworm AS api
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_SYSTEM_PYTHON=1
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
COPY --from=core /wheels /wheels
COPY api/pyproject.toml api/uv.lock ./
COPY api/src ./src
COPY --chmod=755 api/docker-entrypoint.sh /usr/local/bin/tabayyun-api
# Install runtime deps from the lock (except the path dependency), then the built wheel, then
# the API itself with --no-deps: its deps are already present, and resolving them would make
# uv follow the ../core/tabayyun-py path source, which does not exist in this image.
RUN uv export --no-dev --no-emit-project --no-emit-package tabayyun-core --frozen -o /tmp/req.txt \
 && uv pip install --no-cache -r /tmp/req.txt /wheels/*.whl \
 && uv pip install --no-cache --no-deps . \
 && rm -rf /wheels
RUN useradd --system --uid 10001 --create-home tabayyun \
 && mkdir -p /data/cache && chown -R tabayyun /data
USER tabayyun
ENV TABAYYUN_ENV=prod TABAYYUN_CACHE_URL=/data/cache
# The commit this image was built from, reported by /api/version; declared last so a new
# commit does not invalidate the cached layers above.
ARG TABAYYUN_COMMIT=unknown
ENV TABAYYUN_COMMIT=${TABAYYUN_COMMIT}
EXPOSE 8000
# api role: probes /healthz; worker role: healthy while the process runs (no HTTP).
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD ["tabayyun-api", "healthcheck"]
# api role: migrate (packaged Alembic scripts), then uvicorn. worker role: python -m tabayyun.jobs.
CMD ["tabayyun-api"]
