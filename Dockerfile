# syntax=docker/dockerfile:1
#
# Husk container image (published to ghcr.io/husk-ai-lab/husk-ai).
# Two stages: a Node stage builds the Studio bundle (apps/studio/dist is
# gitignored, so it must be built), and a uv/Python stage runs the backend
# which serves that bundle. Run:
#
#   docker run --rm -p 7654:7654 -v husk-data:/data ghcr.io/husk-ai-lab/husk-ai
#
# then open http://localhost:7654 (seed sample data with:
#   docker exec <container> husk-studio demo).

# --- Stage 1: build the Studio --------------------------------------------------
FROM node:20-bookworm-slim AS studio
WORKDIR /app
RUN corepack enable
COPY . .
RUN corepack pnpm install --frozen-lockfile
RUN corepack pnpm --filter studio build   # -> /app/apps/studio/dist

# --- Stage 2: Python runtime ----------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS runtime
WORKDIR /app
ENV HUSK_HOME=/data \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Put the workspace venv on PATH so `docker exec <container> husk-studio demo`
    # works — the form the docs use and the one anyone would type. Without it
    # the binary lives only in /app/.venv/bin and exec fails with
    # "executable file not found in $PATH".
    PATH="/app/.venv/bin:$PATH"
# Install the Python workspace (runtime deps only) from the committed lockfile.
COPY . .
RUN uv sync --frozen --all-packages --no-dev
# Bundle the optional Postgres drivers so HUSK_DB_URL=postgresql+asyncpg://… works
# out of the box (the compose "postgres" profile relies on this).
RUN uv pip install "asyncpg>=0.29" "psycopg[binary]>=3.1"
# Drop in the Studio bundle the backend serves at apps/studio/dist.
COPY --from=studio /app/apps/studio/dist ./apps/studio/dist
VOLUME ["/data"]
EXPOSE 7654
# --no-sync: the env is already synced above; don't re-resolve at boot.
CMD ["uv", "run", "--no-sync", "husk-studio", "start", "--host", "0.0.0.0", "--no-open-browser"]
