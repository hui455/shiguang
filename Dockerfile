FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_TRANSPORT=http \
    IMAGE_OUTPUT_DIR=/data/images \
    HOST=0.0.0.0 \
    PORT=8000

RUN pip install "mcp>=1.10,<2" "openai>=1.60" "pillow>=10" "uvicorn>=0.32" \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /data/images && chown -R app /data

WORKDIR /app
COPY --chmod=644 plugins/openai-image-gen/server.py /app/server.py
COPY --chmod=644 plugins/openai-image-gen/admin.py /app/admin.py
COPY --chmod=755 plugins/openai-image-gen/admin_assets /app/admin_assets

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["python", "server.py"]
