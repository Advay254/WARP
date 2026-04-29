FROM node:20-slim AS base

# ── System deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    supervisor curl wget git \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# ── Evolution API ─────────────────────────────────────────────────────────────
WORKDIR /evolution
RUN npm install -g @evolution-api/evolution-api@2.2.3 2>/dev/null || \
    npm install -g evolution-api 2>/dev/null || true

# Install Evolution API directly from source as fallback
RUN git clone --depth 1 --branch v2.2.3 https://github.com/EvolutionAPI/evolution-api.git /evolution-src 2>/dev/null || \
    git clone --depth 1 https://github.com/EvolutionAPI/evolution-api.git /evolution-src
WORKDIR /evolution-src
RUN npm install && npx prisma generate && npm run build && npm prune --production

# ── Python proxy ──────────────────────────────────────────────────────────────
RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/
COPY config/ /config/

# ── Supervisord config ────────────────────────────────────────────────────────
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Storage dirs
RUN mkdir -p /evolution-src/store/instances /app/data && \
    chmod -R 777 /evolution-src/store /app/data

ENV PORT=7860
ENV EVOLUTION_PORT=8080

EXPOSE 7860

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
