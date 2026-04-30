#!/bin/sh
# start-evolution.sh
# Runs DB migrations then starts Evolution API.
# NEVER uses set -e — migration failures must not abort the process.

cd /evolution-src

echo "[WARP] Preparing Prisma migrations..."
mkdir -p ./prisma/migrations
cp -rf ./prisma/postgresql-migrations/. ./prisma/migrations/ 2>/dev/null
echo "[WARP] Migration files staged."

echo "[WARP] Running: prisma migrate deploy..."
timeout 120 npx prisma migrate deploy --schema ./prisma/postgresql-schema.prisma 2>&1
MIGRATE_EXIT=$?

if [ "$MIGRATE_EXIT" = "0" ]; then
  echo "[WARP] Migrations applied successfully."
else
  echo "[WARP] Migration exited with code $MIGRATE_EXIT (may already be up to date — continuing)."
fi

echo "[WARP] Starting Evolution API on port 8080..."
exec node dist/main.js
