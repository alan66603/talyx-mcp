# Product image: the talyx proxy only. Bring your own MCP server —
# base your server's image on this, or install talyx alongside it.
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Prometheus /metrics endpoint (default port; override with TALYX_METRICS_PORT).
EXPOSE 9464

# Usage: docker run ... talyx -- <your server command>
ENTRYPOINT ["talyx"]
