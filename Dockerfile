# jim — the seller, containerized (Horizon 1: the live deployment).
#
# One image runs the whole surface: paid research routes, the storefront, the
# proof page, discovery (manifest + agent card), the mock vendors, and — when
# MONITOR_AUTOSTART=true — the monitor scheduler in-process. State lives in
# Postgres (DATABASE_URL); secrets arrive as env vars from the platform's
# secret store, never baked into the image. See docs/DEPLOY.md for the runbook.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer first (cache-friendly), then the project itself.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 4021

# Liveness: the free /health route (no payment, no upstream calls).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4021/health', timeout=4).status == 200 else 1)"]

CMD ["jim-seller"]
