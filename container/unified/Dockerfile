# Unified LaTeX-On-HTTP container.
#
# Bundles PostgreSQL, the cache process, and the main HTTP API
# into a single container for simple self-hosting deployments.
#
# Base: yoant/latexonhttp-python:debian (TexLive + Python)
# Added: PostgreSQL 17, supervisord, goose migrations

FROM yoant/latexonhttp-python:debian
LABEL maintainer="LaTeX-On-HTTP unified container"

ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8
ENV DEBIAN_FRONTEND=noninteractive

# ── Install PostgreSQL 17 + supervisord ──────────────────────────────
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends \
        gnupg2 lsb-release curl ca-certificates supervisor \
    && echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/postgresql.gpg \
    && apt-get update -qq \
    && apt-get install -y --no-install-recommends \
        postgresql-17 postgresql-client-17 \
    && apt-get autoremove --purge -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Install goose migration tool ─────────────────────────────────────
RUN curl -fsSL \
        https://raw.githubusercontent.com/pressly/goose/master/install.sh \
        | sh

# ── Application setup ────────────────────────────────────────────────
RUN mkdir -p /app/latex-on-http
WORKDIR /app/latex-on-http/

COPY app.py app_cache.py Makefile pyproject.toml poetry.lock /app/latex-on-http/
COPY ./latexonhttp/ /app/latex-on-http/latexonhttp/

RUN make install

# ── Migrations ────────────────────────────────────────────────────────
RUN mkdir -p /app/latex-on-http/tools/migrations
COPY ./tools/migrations/ /app/latex-on-http/tools/migrations/
ENV GOOSE_MIGRATION_DIR=/app/latex-on-http/tools/migrations
ENV GOOSE_DRIVER=postgres

# ── Runtime directories ──────────────────────────────────────────────
RUN mkdir -p /var/run/postgresql \
    && chown postgres:postgres /var/run/postgresql \
    && mkdir -p /var/log/supervisor \
    && mkdir -p /data/postgres

# ── Copy config and entrypoint ───────────────────────────────────────
COPY ./container/unified/supervisord.conf /etc/supervisor/conf.d/latexonhttp.conf
COPY ./container/unified/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# ── Environment defaults (overridable at runtime) ────────────────────
ENV POSTGRES_USER=latexonhttp
ENV POSTGRES_PASSWORD=latexdev
ENV POSTGRES_DB=latexonhttp
ENV PGDATA=/data/postgres
ENV GOOSE_DBSTRING=postgres://latexonhttp:latexdev@localhost:5432/latexonhttp
ENV CACHE_HOST=localhost
ENV LOGGING_LEVEL=INFO
ENV DEFAULT_COMPILE_TIMEOUT=30

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
