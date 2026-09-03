#!/usr/bin/env sh
# Build the React Mini App before starting the same Python server deployment.
set -eu

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required to build miniapp/. Configure the host build image with Node 22." >&2
  exit 1
fi

if ! command -v pnpm >/dev/null 2>&1; then
  corepack enable
  corepack prepare pnpm@10.15.1 --activate
fi

cd "$(dirname "$0")/miniapp"
pnpm install --frozen-lockfile
pnpm build
