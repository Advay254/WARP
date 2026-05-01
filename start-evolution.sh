#!/bin/sh
# start-evolution.sh
# Supabase has two pooler modes:
#   port 6543 = transaction mode (blocks advisory locks - Prisma hangs)
#   port 5432 = session mode    (supports advisory locks - Prisma works)
# We switch to 5432 only for the schema push step.

cd /evolution-src

echo "[WARP] Deriving session-mode URL (port 5432)..."
SCHEMA_URL=$(echo "$DATABASE_CONNECTION_URI" \
  | sed 's/:6543\//:5432\//' \
  | sed 's/[?&]pgbouncer=true//' \
  | sed 's/[?&]connection_limit=1//')

echo "[WARP] Running prisma db push via session-mode pooler..."
DATABASE_URL="$SCHEMA_URL" npx prisma db push \
  --schema ./prisma/postgresql-schema.prisma \
  --skip-generate \
  --accept-data-loss 2>&1

PUSH_EXIT=$?
if [ "$PUSH_EXIT" = "0" ]; then
  echo "[WARP] Schema synced successfully."
else
  echo "[WARP] db push exited $PUSH_EXIT — may already be in sync, continuing."
fi

echo "[WARP] Starting Evolution API on port 8080..."
exec node dist/main.js
