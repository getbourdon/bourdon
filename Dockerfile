# Bourdon self-host — a free, single-instance, always-on MCP server.
# Build:  docker build -t bourdon .
# Run:    docker run -d -p 7500:7500 -v bourdon-data:/data --name bourdon bourdon
# Token:  docker logs bourdon        # printed once on first boot
# Full guide: docs/SELF_HOST.md
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Bourdon" \
      org.opencontainers.image.description="Recognition-first agent memory — self-hostable MCP server" \
      org.opencontainers.image.source="https://github.com/getbourdon/bourdon" \
      org.opencontainers.image.url="https://bourdon.ai" \
      org.opencontainers.image.licenses="BUSL-1.1"

WORKDIR /app
COPY . /app

# Install the package with the [federation] extra — this pulls fastmcp +
# uvicorn (the HTTP MCP host, required for `serve --transport http`) and httpx
# (the peer client for `serve --peer`). The base install omits these, so the
# HTTP transport needs this extra.
RUN pip install --no-cache-dir '.[federation]' \
 && mkdir -p /data \
 && chmod +x /app/deploy/docker-entrypoint.sh

# Defaults target the persistent /data volume; override any at `docker run -e`.
ENV BOURDON_FEDERATION_CONFIG=/data/.bourdon/federation.yaml \
    BOURDON_AUDIT_PATH=/data/.bourdon/audit.jsonl \
    BOURDON_LIBRARY=/data/agent-library \
    BOURDON_HOST=0.0.0.0 \
    BOURDON_PORT=7500

VOLUME ["/data"]
EXPOSE 7500

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "/app/deploy/healthcheck.py"]

ENTRYPOINT ["/app/deploy/docker-entrypoint.sh"]
