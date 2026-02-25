# Cloudflare Containers Deployment Guide

## What are Cloudflare Containers?

Cloudflare Containers allow you to run Docker containers on Cloudflare's global network. Unlike traditional Workers (which run JavaScript/WebAssembly), Containers let you run any application that can be containerized—like your LaTeX compilation service.

**Key features:**
- **Global deployment**: Deploy once, runs everywhere on Cloudflare's edge
- **On-demand scaling**: Containers start automatically when requests arrive
- **Cost-effective**: Containers sleep after inactivity (5 minutes in your config), so you only pay when running
- **Cold starts**: Typically 2-3 seconds to start a container instance

## Prerequisites

Before deploying, ensure you have:

1. **A Cloudflare account** (paid plan required - Containers are in public beta)
   - Sign up at https://dash.cloudflare.com/
   - Containers require a paid plan (not free tier)

2. **Docker installed and running**
   - **macOS**: Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) or [Colima](https://github.com/abiosoft/colima)
   - **Linux**: Install Docker via your package manager
   - **Windows**: Install Docker Desktop
   
   Verify Docker is running:
   ```bash
   docker info
   ```
   If this command works, Docker is running.

3. **Node.js and npm** (for Wrangler CLI)
   - Check: `node --version` and `npm --version`
   - Install from https://nodejs.org/ if needed

4. **Wrangler CLI** (already in your project dependencies)
   - Will be installed via `npm install` in the worker directory

## Step-by-Step Deployment

### Step 1: Install Dependencies

Navigate to the worker directory and install npm packages:

```bash
cd container/cloudflare/worker
npm install
```

This installs:
- `wrangler` (Cloudflare CLI)
- `@cloudflare/containers` (Container SDK)
- TypeScript and type definitions

### Step 2: Authenticate with Cloudflare

Log in to your Cloudflare account using Wrangler:

```bash
npx wrangler login
```

This will:
- Open your browser to Cloudflare's login page
- Ask you to authorize Wrangler
- Store authentication credentials locally

**Verify authentication:**
```bash
npx wrangler whoami
```

This should show your Cloudflare account email and account ID.

**Do you need `account_id` in `wrangler.toml`?**

**It depends:**

- **If you have ONE account**: Optional. Wrangler automatically infers your account ID from your authentication token.
- **If you have MULTIPLE accounts**: **Required.** You must specify which account to use, otherwise Wrangler may deploy to the wrong account.

**To add it:**
```toml
account_id = "your-account-id-here"
```

You can find your Account ID:
- From `wrangler whoami` output (shows all accounts)
- In Cloudflare Dashboard → Overview → Account ID (right sidebar)

**Note**: If `wrangler whoami` shows multiple accounts, copy the Account ID for the account you want to deploy to and add it to `wrangler.toml`.

### Step 3: Verify Docker is Running

**Critical**: Docker must be running during deployment. Wrangler uses Docker to build your container image.

```bash
docker info
```

If this fails, start Docker Desktop (or Colima) and try again.

### Step 4: Deploy Your Container

From the `container/cloudflare/worker` directory, run:

```bash
npm run deploy
```

Or directly:
```bash
npx wrangler deploy
```

**What happens during deployment:**

1. **Worker deployment**: Wrangler uploads your TypeScript Worker code
2. **Docker build**: Wrangler builds your Docker image using `../Dockerfile`
   - Builds from the repo root (as specified in `image_build_context`)
   - This may take several minutes (your image includes TeX Live, ~6.7 GB)
3. **Image push**: Pushes the built image to Cloudflare's Container Registry
4. **Durable Object migration**: Creates the SQLite-backed Durable Object storage (first deploy only)

**Expected output:**
```
✨ Compiled Worker successfully
📦 Building container image...
🐳 Building Docker image...
📤 Uploading container image...
✨ Deployed Worker successfully
```

### Step 5: Wait for Provisioning

**Important**: After the first deployment, wait **5-10 minutes** for Cloudflare to provision your container infrastructure. This is longer than regular Workers because containers require more setup.

### Step 6: Verify Deployment

Check that your containers are deployed:

```bash
# List running container instances
npm run containers:list

# List container images in registry
npm run containers:images
```

You should see:
- Your Worker deployed
- Container image in the registry
- Container instances ready (or stopped, waiting for requests)

### Step 7: Test Your Deployment

Your Worker will be available at:
```
https://latex-on-http.<your-subdomain>.workers.dev
```

**Test the health endpoint:**
```bash
curl https://latex-on-http.<your-subdomain>.workers.dev/health
```

Should return: `ok`

**Test LaTeX compilation:**
Send a POST request to your Worker endpoint with LaTeX content (your Worker will route it to a container instance).

## Understanding Your Deployment

### Architecture

```
HTTP Request
    ↓
Cloudflare Worker (TypeScript)
    ↓
Durable Object (manages container lifecycle)
    ↓
Container Instance (Docker - Gunicorn + Flask + LaTeX)
    ↓
Response (PDF)
```

### Container Lifecycle

1. **Cold start**: First request triggers container start (~2-3 seconds)
2. **Active**: Container handles requests, stays alive for 5 minutes after last request
3. **Sleep**: After 5 minutes idle, container stops automatically
4. **Warm start**: Next request starts container again (faster than cold start)

### Load Balancing

- Your Worker load-balances across **5 container instances** using `getRandom()`
- Each instance runs **2 Gunicorn workers** (from Dockerfile CMD)
- Maximum **5 containers** can run simultaneously (per `max_instances`)

### Instance Configuration

- **Instance type**: `standard-1`
  - 1/2 vCPU
  - 4 GiB RAM
  - 8 GB disk (sufficient for your ~6.7 GB image + temp files)

## Troubleshooting

### "Docker daemon not running"

**Solution**: Start Docker Desktop or Colima, then retry deployment.

### "Authentication failed"

**Solution**: Run `npx wrangler login` again.

### "Build failed" or "Image too large"

**Solution**: 
- Check Docker build locally: `docker build -f container/cloudflare/Dockerfile .`
- Verify base image exists: `docker pull yoant/latexonhttp-python:debian`
- Check disk space: Ensure you have enough space for the ~6.7 GB image

### "Container not responding"

**Solution**:
- Wait 5-10 minutes after first deployment
- Check container logs: `npx wrangler tail`
- Verify health endpoint: `curl https://<your-worker-url>/health`

### "Migration error" on subsequent deploys

**Solution**: Remove the `[[migrations]]` section from `wrangler.toml` after first successful deploy (migrations only run once).

## Monitoring and Logs

**View Worker logs:**
```bash
npx wrangler tail
```

**View container status:**
```bash
npm run containers:list
```

**View container images:**
```bash
npm run containers:images
```

## Cost Considerations

- **Pricing**: Cloudflare Containers are billed per:
  - Container runtime (time containers are running)
  - Storage (container images in registry)
  - Data transfer
  
- **Cost optimization**: Your `sleepAfter = "5m"` setting helps minimize costs by stopping idle containers quickly.

- **Check pricing**: Visit https://developers.cloudflare.com/containers/pricing/ for current rates

## Next Steps

1. **Custom domain**: Configure a custom domain in Cloudflare dashboard
2. **Environment variables**: Add secrets if needed: `npx wrangler secret put <KEY>`
3. **Monitoring**: Set up alerts in Cloudflare dashboard
4. **Scaling**: Adjust `max_instances` if you need more throughput

## Useful Commands Reference

```bash
# Deploy
npm run deploy

# Local development (requires Docker)
npm run dev

# Check deployment status
npm run containers:list
npm run containers:images

# View logs
npx wrangler tail

# Update secrets
npx wrangler secret put MY_SECRET

# Check authentication
npx wrangler whoami

# Logout
npx wrangler logout
```

## Additional Resources

- **Official Docs**: https://developers.cloudflare.com/containers/
- **Wrangler Docs**: https://developers.cloudflare.com/workers/wrangler/
- **Cloudflare Dashboard**: https://dash.cloudflare.com/
- **Support**: https://community.cloudflare.com/
