FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN uv pip install --system ".[postgres,pdf]"

ENV OBLAG_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["oblag", "serve", "--host", "0.0.0.0", "--with-scheduler"]
