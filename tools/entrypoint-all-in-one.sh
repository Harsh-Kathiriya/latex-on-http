#!/bin/bash

set -o nounset # Exit on undeclared vars
set -o errexit # Exit on command error

# Initialize PostgreSQL if data directory is empty
if [ ! -s /var/lib/postgresql/data/PG_VERSION ]; then
    echo "Initializing PostgreSQL database..."
    sudo -u postgres /usr/lib/postgresql/17/bin/initdb -D /var/lib/postgresql/data
    
    # Configure PostgreSQL
    echo "host all all 0.0.0.0/0 md5" >> /var/lib/postgresql/data/pg_hba.conf
    echo "listen_addresses = '*'" >> /var/lib/postgresql/data/postgresql.conf
    
    # Start PostgreSQL temporarily to create database and user
    sudo -u postgres /usr/lib/postgresql/17/bin/pg_ctl -D /var/lib/postgresql/data -l /var/lib/postgresql/data/logfile start
    
    # Wait for PostgreSQL to be ready
    echo "Waiting for PostgreSQL to start..."
    for i in {1..30}; do
        if sudo -u postgres psql -c "SELECT 1" > /dev/null 2>&1; then
            echo "PostgreSQL is ready!"
            break
        fi
        if [ $i -eq 30 ]; then
            echo "PostgreSQL failed to start"
            exit 1
        fi
        sleep 1
    done
    
    # Create database and user
    sudo -u postgres psql -c "CREATE USER ${POSTGRES_USER} WITH PASSWORD '${POSTGRES_PASSWORD}';" || true
    sudo -u postgres psql -c "CREATE DATABASE ${POSTGRES_DB} OWNER ${POSTGRES_USER};" || true
    sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${POSTGRES_DB} TO ${POSTGRES_USER};" || true
    
    # Stop PostgreSQL (supervisor will start it)
    sudo -u postgres /usr/lib/postgresql/17/bin/pg_ctl -D /var/lib/postgresql/data stop
fi

# Start supervisor to manage all processes
echo "Starting supervisor..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
