FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
        'mcp>=1.0' \
        'httpx>=0.27' \
        'anyio>=4.4'

COPY server.py /app/server.py

ENV PYTHONUNBUFFERED=1 \
    SAEBOOKS_MCP_TRANSPORT=sse \
    SAEBOOKS_MCP_HOST=0.0.0.0 \
    SAEBOOKS_MCP_PORT=8000

EXPOSE 8000

CMD ["python", "/app/server.py"]
