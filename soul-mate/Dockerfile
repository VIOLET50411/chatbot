# syntax=docker/dockerfile:1.7

FROM node:20-bookworm-slim AS frontend-build
WORKDIR /build/web/frontend
COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/frontend/ ./
RUN npm run build

FROM python:3.11-slim-bookworm AS python-deps
ARG TORCH_VERSION=2.13.0+cpu
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
    && pip install --index-url "$TORCH_INDEX_URL" "torch==$TORCH_VERSION" \
    && pip install -r /tmp/requirements.txt

FROM python:3.11-slim-bookworm AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    DATA_DIR=/app/data \
    CHARACTER_REGISTRY_DIR=/app/data/registry \
    HF_HOME=/app/data/models/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/app/data/models/sentence-transformers
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin soulmate
COPY --from=python-deps /opt/venv /opt/venv
WORKDIR /app
COPY --chown=soulmate:soulmate . .
COPY --from=frontend-build --chown=soulmate:soulmate /build/web/static /app/web/static
RUN mkdir -p /app/data \
    && chown -R soulmate:soulmate /app/data
USER soulmate
EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=5s --start-period=90s --retries=5 \
    CMD python -c "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3); assert r.status == 200 and json.load(r).get('status') == 'ok'"
CMD ["python", "main.py"]
