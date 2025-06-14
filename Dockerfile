FROM debian:trixie-slim AS builder
RUN apt-get update && apt-get install -y ca-certificates git && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app
ENV UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON_INSTALL_DIR=/app/python
RUN uv python install 3.13
RUN uv venv --python 3.13 /app/.venv
COPY pyproject.toml uv.lock ./
ENV VIRTUAL_ENV=/app/.venv
RUN uv sync --locked --no-install-project --no-editable --no-dev
COPY . .
RUN uv sync --locked --no-editable --no-dev
RUN GIT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown") && \
    if [ "$GIT_COMMIT" != "unknown" ] && ! git diff --quiet 2>/dev/null; then \
        GIT_COMMIT="${GIT_COMMIT}-dirty"; \
    fi && \
    echo "${GIT_COMMIT}" > /app/.venv/.git_commit && \
    echo "Built with commit: ${GIT_COMMIT}"


FROM debian:trixie-slim
COPY --from=builder /etc/ssl/certs /etc/ssl/certs
COPY --from=builder /usr/share/ca-certificates /usr/share/ca-certificates
COPY --from=builder /app/python /app/python
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/app.py /app/app.py
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app"
CMD ["adsblol-collector"]
