# Listing image for MCP directories (Glama): talyx wrapping the bundled mock
# server, so a crawler's initialize / tools/list goes through the proxy and gets
# an answer. To proxy your own server, use deploy/docker/Dockerfile instead.
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
# pyproject force-includes these into the wheel for `talyx dashboard up`.
COPY alerts ./alerts
COPY dashboards ./dashboards
RUN pip install --no-cache-dir .

# One process per container: the wrapper serves /metrics itself, no hub.
ENV TALYX_HUB=off

EXPOSE 9464

ENTRYPOINT ["talyx", "--", "python", "-m", "talyx.mock.server"]
