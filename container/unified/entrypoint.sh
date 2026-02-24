#!/bin/bash
set -e

PGDATA="${PGDATA:-/data/postgres}"
POSTGRES_USER="${POSTGRES_USER:-latexonhttp}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-latexdev}"
POSTGRES_DB="${POSTGRES_DB:-latexonhttp}"

# ── Initialise PostgreSQL data directory if empty ────────────────────
if [ ! -s "$PGDATA/PG_VERSION" ]; then
    echo "[init] Initialising PostgreSQL data directory..."
    chown postgres:postgres "$PGDATA"
    su - postgres -c "/usr/lib/postgresql/17/bin/initdb -D '$PGDATA' --auth-local=trust --auth-host=md5"

    # Start temporarily to create role + database
    su - postgres -c "/usr/lib/postgresql/17/bin/pg_ctl -D '$PGDATA' -l /tmp/pg_init.log start -w"

    su - postgres -c "psql -c \"CREATE ROLE $POSTGRES_USER WITH LOGIN PASSWORD '$POSTGRES_PASSWORD';\""
    su - postgres -c "psql -c \"CREATE DATABASE $POSTGRES_DB OWNER $POSTGRES_USER;\""

    su - postgres -c "/usr/lib/postgresql/17/bin/pg_ctl -D '$PGDATA' stop -w"
    echo "[init] PostgreSQL initialised."
fi

# Ensure ownership even on pre-existing volumes
chown -R postgres:postgres "$PGDATA"

# ── Start all services via supervisord ───────────────────────────────
echo "[init] Starting all services (PostgreSQL, cache, API)..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/latexonhttp.conf
