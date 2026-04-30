#!/bin/sh
cd /evolution-src

echo "==> Preparing migrations..."
mkdir -p ./prisma/migrations
cp -r ./prisma/postgresql-migrations/. ./prisma/migrations/ 2>/dev/null && echo "==> Migrations copied" || echo "==> Migration copy skipped (already exist)"

echo "==> Running prisma migrate deploy..."
npx prisma migrate deploy --schema ./prisma/postgresql-schema.prisma 2>&1 || echo "==> Migration warning (may already be applied)"

echo "==> Starting Evolution API on port 8080..."
exec node dist/main.js
