#!/bin/bash
set -e
cd /evolution-src

echo "==> Preparing Prisma migrations..."
rm -rf ./prisma/migrations
cp -r ./prisma/postgresql-migrations ./prisma/migrations

echo "==> Running database migrations..."
npx prisma migrate deploy --schema ./prisma/postgresql-schema.prisma

echo "==> Migrations complete. Starting Evolution API..."
exec node dist/main.js
