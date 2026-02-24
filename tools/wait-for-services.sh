#!/bin/bash

# Wait for PostgreSQL to be ready
echo "Waiting for PostgreSQL..."
for i in {1..60}; do
    if PGPASSWORD=latexdev psql -h localhost -U latexonhttp -d latexonhttp -c "SELECT 1" > /dev/null 2>&1; then
        echo "PostgreSQL is ready!"
        break
    fi
    if [ $i -eq 60 ]; then
        echo "PostgreSQL failed to start after 60 seconds"
        exit 1
    fi
    sleep 1
done

# Wait for cache service to be ready
echo "Waiting for cache service..."
for i in {1..60}; do
    if nc -z localhost 10000 > /dev/null 2>&1; then
        echo "Cache service is ready!"
        break
    fi
    if [ $i -eq 60 ]; then
        echo "Warning: Cache service may not be ready, but continuing..."
        break
    fi
    sleep 1
done

echo "All services are ready!"
