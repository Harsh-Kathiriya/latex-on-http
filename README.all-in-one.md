# All-in-One Docker Container

This setup combines the LaTeX-on-HTTP main service, cache service, and PostgreSQL database into a single Docker container for simplified self-hosting.

## Quick Start

### Build and Run

```bash
# Build the all-in-one image
docker build -f Dockerfile.all-in-one -t latex-on-http-all-in-one .

# Run the container
docker run -d \
  --name latex-on-http \
  -p 8080:8080 \
  -v latex-on-http-postgres:/var/lib/postgresql/data \
  -v latex-on-http-cache:/app/latex-on-http/cache \
  latex-on-http-all-in-one
```

Or using docker-compose:

```bash
docker-compose -f docker-compose.all-in-one.yml up -d
```

The service will be available at `http://localhost:8080`.

## What's Included

- **PostgreSQL 17**: Database for storing job metadata
- **Cache Service**: ZeroMQ-based caching layer (ports 10000, 10001)
- **Main LaTeX Service**: HTTP API server (port 8080)
- **Supervisor**: Process manager to run all services

## Environment Variables

You can customize the setup using environment variables:

- `POSTGRES_USER`: PostgreSQL username (default: `latexonhttp`)
- `POSTGRES_PASSWORD`: PostgreSQL password (default: `latexdev`)
- `POSTGRES_DB`: Database name (default: `latexonhttp`)
- `CACHE_HOST`: Cache service host (default: `localhost`)
- `LOGGING_LEVEL`: Log level (default: `INFO`)
- `DEFAULT_COMPILE_TIMEOUT`: Compilation timeout in seconds (default: `15`)
- `KEEP_WORKSPACE_DIR_ON_ERROR`: Keep workspace on error (default: `1`)

## Volumes

- `/var/lib/postgresql/data`: PostgreSQL data directory (persist database)
- `/app/latex-on-http/cache`: Cache storage directory (optional, for persistence)

## Process Management

All services are managed by Supervisor:
- PostgreSQL starts first (priority 100)
- Cache service starts second (priority 200)
- Main LaTeX service starts last (priority 300) after running database migrations

## Logs

View logs for all services:

```bash
docker logs latex-on-http
```

Or view individual service logs inside the container:

```bash
docker exec latex-on-http tail -f /var/log/supervisor/latex.out.log
docker exec latex-on-http tail -f /var/log/supervisor/cache.out.log
docker exec latex-on-http tail -f /var/log/supervisor/postgresql.out.log
```

## Advantages

- **Simplified deployment**: Only one container to manage
- **Easier self-hosting**: No need to coordinate multiple containers
- **Resource efficient**: Shared base image and dependencies

## Limitations

- **Scaling**: Cannot scale individual services independently
- **Maintenance**: All services restart together
- **Resource isolation**: Less isolation between services

For production deployments with high availability requirements, consider using the multi-container setup (`docker-compose.yml`).
