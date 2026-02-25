# Quick Start: Deploy to Cloudflare Containers

## Prerequisites Checklist

- [ ] Cloudflare account (paid plan required)
- [ ] Docker installed and running (`docker info` works)
- [ ] Node.js installed (`node --version` works)

## Deployment Steps (5 minutes)

### 1. Install dependencies
```bash
cd container/cloudflare/worker
npm install
```

### 2. Login to Cloudflare
```bash
npx wrangler login
```

### 3. Verify Docker is running
```bash
docker info
```
If this fails, start Docker Desktop/Colima.

### 4. Deploy!
```bash
npm run deploy
```

### 5. Wait 5-10 minutes
Containers take longer to provision than Workers.

### 6. Verify deployment
```bash
npm run containers:list
curl https://latex-on-http.<your-subdomain>.workers.dev/health
```

## Your Worker URL

After deployment, your Worker will be available at:
```
https://latex-on-http.<your-subdomain>.workers.dev
```

Replace `<your-subdomain>` with your Cloudflare account subdomain (shown after deployment).

## Common Issues

**"Docker daemon not running"**
→ Start Docker Desktop or Colima

**"Authentication failed"**
→ Run `npx wrangler login` again

**"Build failed"**
→ Check Docker can build locally: `docker build -f container/cloudflare/Dockerfile .`

## Need Help?

See `DEPLOYMENT.md` for detailed explanations and troubleshooting.
