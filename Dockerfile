# syntax=docker/dockerfile:1
# Multi-arch (linux/amd64, linux/arm64), Debian slim (glibc): every dependency ships manylinux
# wheels, so nothing compiles. Targets: runtime (default) and test.
# Not Alpine: under musl, lxml in the extraction thread pool corrupted the heap and the nightly
# run died with SIGSEGV in ld-musl's allocator (2026-11-24).

FROM ghcr.io/astral-sh/uv:0.12 AS uv

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
COPY --from=uv /uv /bin/uv
WORKDIR /app

FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM deps AS build
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM build AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable
COPY tests ./tests
COPY config ./config
ENV PATH=/app/.venv/bin:$PATH
CMD ["pytest", "-q"]

FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.source="https://github.com/warroyo/content-curator" \
      org.opencontainers.image.description="Self-hosted content curator"
RUN useradd --uid 1000 --no-create-home --home-dir /data --shell /usr/sbin/nologin curator \
    && mkdir -p /data /config && chown curator /data
COPY --from=build /app/.venv /app/.venv
COPY config /config.example
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    CURATOR_DATA_DIR=/data \
    CURATOR_CONFIG_DIR=/config
USER curator
WORKDIR /data
VOLUME ["/data"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["curator"]
EXPOSE 8090
CMD ["serve"]
