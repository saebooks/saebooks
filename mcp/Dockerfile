FROM python:3.12-slim

WORKDIR /app

# The standalone MCP container runs the same FastMCP instance as the
# in-tree mount, just hosted in its own process. Install the saebooks
# package so the import path ``saebooks.mcp.server`` resolves. Build
# context MUST be the repo root (see compose ``build.context: .``).
COPY pyproject.toml /app/pyproject.toml
COPY saebooks /app/saebooks
RUN pip install --no-cache-dir /app

COPY mcp/server.py /app/server.py

ENV PYTHONUNBUFFERED=1 \
    SAEBOOKS_MCP_TRANSPORT=sse \
    SAEBOOKS_MCP_HOST=0.0.0.0 \
    SAEBOOKS_MCP_PORT=8000

EXPOSE 8000

CMD ["python", "/app/server.py"]
