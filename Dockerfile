# =============================================================================
# (c) Clearview Geographic LLC — All Rights Reserved | Est. 2018
# CVG Neuron — Artificial Intelligence Engine
# Author: Alex Zelenski, GISP | azelenski@clearviewgeographic.com
# =============================================================================
FROM python:3.13-slim AS base

LABEL maintainer="Alex Zelenski, GISP <azelenski@clearviewgeographic.com>"
LABEL org.opencontainers.image.title="CVG Neuron"
LABEL org.opencontainers.image.description="CVG Neuron — Artificial Intelligence Engine for Clearview Geographic LLC"
LABEL org.opencontainers.image.vendor="Clearview Geographic LLC"
LABEL org.opencontainers.image.licenses="Proprietary"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NEURON_DATA_DIR=/app/data

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    bash \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

COPY . .
RUN pip install --no-cache-dir -e ".[web]"

# Data directory for persistent memory (episodic/semantic/procedural JSON)
RUN mkdir -p /app/data/memory

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

RUN useradd -m -u 1001 neuron && \
    chown -R neuron:neuron /app
USER neuron

EXPOSE 8095

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
  CMD curl -fsS http://localhost:8095/health | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('status') in ('ok','operational') else 1)" || exit 1

# Entrypoint: register cvg-neuron model with Ollama, then start FastAPI
CMD ["/bin/bash", "/app/entrypoint.sh"]
