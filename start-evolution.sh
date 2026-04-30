#!/bin/sh
# start-evolution.sh
# Runs DB migrations then starts Evolution API.
# NEVER uses set -e — migration failures must not abort the process.

cd /evolution-src

echo "[WARP] Preparing Prisma migrations..."
mkdir -p ./prisma/migrations
cp -rf ./prisma/postgresql-migrations/. ./prisma/migrations/ 2>/dev/null
echo "[WARP] Migration files staged."

echo "[WARP] Running prisma db push (pgbouncer-compatible schema sync)..."
npx prisma db push \
  --schema ./prisma/postgresql-schema.prisma \
  --skip-generate \
  --accept-data-loss 2>&1
PUSH_EXIT=$?

if [ "$PUSH_EXIT" = "0" ]; then
  echo "[WARP] Schema synced successfully."
else
  echo "[WARP] db push exited with code $PUSH_EXIT — tables may already exist, continuing."
fi

echo "[WARP] Starting Evolution API on port 8080..."
exec node dist/main.js
