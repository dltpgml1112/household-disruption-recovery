# Hosted demo: MCP server (/mcp), agent API (/api) and the web simulator (/) on one port.
# Local development does not need this; see README "Run it".
#
#   docker build -t hdr . && docker run -p 7860:7860 -e GEMINI_API_KEY=... hdr
#
# Without GEMINI_API_KEY the rule agent answers every turn.

FROM node:22-slim AS simulator
WORKDIR /web
COPY apps/simulator/package.json apps/simulator/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/simulator/ ./
ENV HDR_STATIC_EXPORT=1 \
    NEXT_PUBLIC_HDR_API=same-origin \
    NEXT_PUBLIC_HDR_HOSTED=1 \
    NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM python:3.12-slim
RUN pip install --no-cache-dir uv==0.12.13
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
COPY fixtures/ fixtures/
RUN uv sync --frozen --no-dev
COPY --from=simulator /web/out /app/static

ENV PATH="/app/.venv/bin:$PATH" \
    HDR_HOST=0.0.0.0 \
    PORT=7860 \
    HDR_STATIC_DIR=/app/static \
    HDR_DB_PATH=/tmp/hdr.sqlite3 \
    PYTHONUNBUFFERED=1
EXPOSE 7860
CMD ["python", "-m", "hdr.hosted"]
