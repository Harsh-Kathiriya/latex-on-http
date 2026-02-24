# Unified LaTeX-On-HTTP Container

Single Docker container that bundles **PostgreSQL 17**, the **ZeroMQ cache process**, and the **main HTTP API** for simple self-hosting on Railway, Fly.io, or any Docker host.

## Quick start

```bash
# From the latex-on-http repo root
docker compose -f container/unified/docker-compose.yml up --build
```

The API is available at **http://localhost:2345** (mapped from container port 8080).

## Build & run manually

```bash
# Build (from repo root)
docker build -f container/unified/Dockerfile -t latexonhttp-unified .

# Run
docker run -p 2345:8080 -v latexonhttp-pgdata:/data/postgres latexonhttp-unified
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_USER` | `latexonhttp` | PostgreSQL user |
| `POSTGRES_PASSWORD` | `latexdev` | PostgreSQL password |
| `POSTGRES_DB` | `latexonhttp` | PostgreSQL database name |
| `LOGGING_LEVEL` | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`) |
| `DEFAULT_COMPILE_TIMEOUT` | `30` | Max seconds per LaTeX compilation |

## What's inside

The container uses **supervisord** to manage three processes:

1. **PostgreSQL 17** — listens on `localhost:5432` (not exposed externally)
2. **Cache process** — ZeroMQ sockets on `localhost:10000` / `localhost:10001`
3. **Gunicorn API** — Flask app on `0.0.0.0:8080` (exposed)

On first boot, the entrypoint initialises the PostgreSQL data directory and creates the database/user. Subsequent starts reuse the existing data from the `/data/postgres` volume.

## Health check

```bash
curl http://localhost:2345/texlive/information
```

## API endpoint

```bash
# Compile a LaTeX document
curl -X POST http://localhost:2345/builds/sync \
  -H "Content-Type: application/json" \
  -d '{
    "compiler": "pdflatex",
    "resources": [{ "main": true, "content": "\\documentclass{article}\\begin{document}Hello World\\end{document}" }]
  }' > output.pdf
```

## Deploying to Railway

1. Point Railway to this repo
2. Set the Dockerfile path to `container/unified/Dockerfile`
3. Set the port to `8080`
4. Optionally attach a persistent volume mounted at `/data/postgres`
5. Set `LATEX_COMPILER_URL` in your API service to the Railway service URL
