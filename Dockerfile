FROM node:20-slim

# ── System deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    supervisor curl git bash \
    && rm -rf /var/lib/apt/lists/*

# ── Evolution API v2.2.3 ──────────────────────────────────────────────────────
RUN git clone --depth 1 --branch 2.2.3 \
    https://github.com/EvolutionAPI/evolution-api.git /evolution-src

WORKDIR /evolution-src

# Install all deps (incl. devDeps for TypeScript), generate Prisma client, build, then prune
RUN npm install && \
    npx prisma generate --schema ./prisma/postgresql-schema.prisma && \
    npm run build && \
    npm prune --production

# ── Python venv ───────────────────────────────────────────────────────────────
RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ── App files ─────────────────────────────────────────────────────────────────
COPY app/ /app/
COPY config/ /config/
COPY start-evolution.sh /start-evolution.sh
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

RUN chmod +x /start-evolution.sh && \
    mkdir -p /evolution-src/store/instances /app/data && \
    chmod -R 777 /evolution-src/store /app/data

ENV PORT=7860
ENV EVOLUTION_PORT=8080

EXPOSE 7860

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
