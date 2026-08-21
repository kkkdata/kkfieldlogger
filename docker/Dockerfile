# Multi-stage build. Build tools stay in the builder stage; the runtime
# stage carries only runtime libraries and runs as a non-root user.
#
# INSTALL_AI=true  (default) also installs the [ai] extra: faster-whisper
#                  voice transcription and opencv video keyframes (~450MB).
# INSTALL_AI=false builds the slim image (NAS/appliance default); both
#                  import sites are guarded and degrade gracefully.

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ARG INSTALL_AI=true

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --no-cache-dir --upgrade pip \
    && if [ "$INSTALL_AI" = "true" ]; then \
        pip install --no-cache-dir --prefix=/install ".[ai]"; \
    else \
        pip install --no-cache-dir --prefix=/install .; \
    fi


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ARG APP_UID=1000
ARG APP_GID=1000

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        postgresql-client \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        shared-mime-info \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g "$APP_GID" kkapp \
    && useradd -u "$APP_UID" -g "$APP_GID" -m kkapp

# Static ffmpeg/ffprobe (~100MB) instead of Debian's ffmpeg package, whose
# dependency tree drags in ~350MB of LLVM/Mesa GPU libraries that a headless
# server never executes. Version-pinned Docker Hub image.
COPY --from=mwader/static-ffmpeg:7.1 /ffmpeg /ffprobe /usr/local/bin/

WORKDIR /app

COPY --from=builder /install /usr/local

COPY pyproject.toml README.md alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY docs ./docs
COPY scripts ./scripts
COPY docker/entrypoint.sh ./docker/entrypoint.sh
COPY docker/worker-entrypoint.sh ./docker/worker-entrypoint.sh

RUN sed -i 's/\r$//' ./docker/entrypoint.sh ./docker/worker-entrypoint.sh \
    && chmod +x ./docker/entrypoint.sh ./docker/worker-entrypoint.sh \
    && chown -R kkapp:kkapp /app

USER kkapp

CMD ["./docker/entrypoint.sh"]
