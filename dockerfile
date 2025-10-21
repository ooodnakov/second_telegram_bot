# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder
ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/root/.local/bin:$PATH"
WORKDIR /workspace

# Install build tools and uv
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc curl && \
    rm -rf /var/lib/apt/lists/* && \
    curl -LsSf https://astral.sh/uv/install.sh | sh

COPY pyproject.toml uv.lock ./
RUN uv sync

FROM python:3.12-slim AS runtime
ENV DEBIAN_FRONTEND=noninteractive \
    VIRTUAL_ENV=/workspace/.venv \
    PATH="/workspace/.venv/bin:/root/.local/bin:$PATH"
WORKDIR /workspace

# Runtime dependencies only
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

RUN useradd --create-home --shell /bin/bash --uid 1001 rem && \
    mkdir -p /workspace && \
    chown -R rem:rem /workspace

COPY --from=builder --chown=rem:rem /workspace/.venv /workspace/.venv
COPY --chown=rem:rem pyproject.toml uv.lock ./
COPY --chown=rem:rem ./bot ./bot
COPY --chown=rem:rem config.ini.example ./config.ini.example

USER rem

ENTRYPOINT ["uv", "run", "python", "-m", "bot.reloader"]
