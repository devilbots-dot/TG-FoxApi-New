# Stage 1: Build the Telegram Mini App once with a pinned Node toolchain.
FROM node:22-bookworm-slim AS miniapp-build

WORKDIR /build/miniapp
RUN corepack enable

COPY miniapp/package.json miniapp/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY miniapp/ ./
RUN pnpm build \
    && test -f /build/miniapp/dist/public/index.html \
    && test -d /build/miniapp/dist/public/assets

# Stage 2: Run the bot, FastAPI server and already-built Mini App together.
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build tooling covers binary Python dependencies when wheels are unavailable.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       ca-certificates \
       libffi-dev \
       libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . ./

# The same FastAPI process serves this output at /app/.
COPY --from=miniapp-build /build/miniapp/dist/public ./miniapp/dist/public
RUN test -f /app/miniapp/dist/public/index.html \
    && test -d /app/miniapp/dist/public/assets

# Render injects PORT; config.py already honors it before API_PORT.
EXPOSE 10000

CMD ["python3", "-m", "server"]
