# Cloudflare deployment runbook

The compiler is an internal service. Production and staging API Workers both
reach the same `latex-on-http` Worker through a `LATEX_COMPILER` service
binding. `workers.dev` and preview URLs stay disabled.

## Before deploying

1. Merge the reviewed compiler, API Worker, and TexPal changes.
2. Create a replacement R2 API key restricted to the
   `latex-compile-cache` bucket. Keep the old key active during the rollout.
3. Put both replacement credentials into one temporary `.env` file, then
   configure them in a single Worker version. Do not update the pair with two
   separate commands because a container restart between them would receive a
   mismatched key pair.

   ```sh
   credential_file="$(mktemp)"
   trap 'rm -f "$credential_file"' EXIT
   chmod 600 "$credential_file"
   ${EDITOR:-vi} "$credential_file"
   npx wrangler secret bulk "$credential_file"
   rm -f "$credential_file"
   trap - EXIT
   ```

   The file must contain exactly `AWS_ACCESS_KEY_ID=...` and
   `AWS_SECRET_ACCESS_KEY=...`. Remove it immediately after the command.

4. Confirm Wrangler is logged into account
   `47bdce30e4337b94e234ac9b31aee19f` and Docker is running.
5. From `container/cloudflare/worker`, run:

   ```sh
   npm ci
   npm run type-check
   npx wrangler deploy --dry-run
   ```

6. Run the Python tests from the repository root. Do not remove the Durable
   Object migration from `wrangler.toml`; Wrangler tracks applied migrations.

## Zero-downtime order

1. Deploy the DeepSpace API Worker to staging and production with its new
   `LATEX_COMPILER` binding while the current compiler is still reachable.
2. Compile a small document through each API environment.
3. From `container/cloudflare/worker`, deploy this Worker/container:

   ```sh
   npm run deploy
   ```

4. Compile a small document through staging to start the replacement
   `instance-0`, then inspect `wrangler containers images list`, `containers
   list`, and `containers info <ID>`. Wait until the instance reports healthy
   and its image matches the image created by this deployment. Wrangler can
   finish the Worker deployment before the container rollout is healthy.
5. Compile a small document through both API environments again. Confirm the
   replacement container mounted R2 and created/restored cache state.
6. Add the cache lifecycle rule. The bucket is dedicated to transient compile
   state, so the rule covers every object while preserving the existing
   incomplete-multipart rule:

   ```sh
   npx wrangler r2 bucket lifecycle add \
     latex-compile-cache expire-compile-cache '' \
     --expire-days 1 --force
   npx wrangler r2 bucket lifecycle list latex-compile-cache
   ```

7. Redeploy TexPal, then compile the same document twice with an edit between
   runs. Verify both builds succeed and the second restores auxiliary state.
8. Confirm the old direct Worker URL and preview URLs are unavailable. Inspect
   logs, container state, and R2 objects before removing the obsolete
   `LATEX_COMPILER_URL` secret/configuration.
9. Revoke the old R2 key only after the new container has successfully mounted
   the bucket. The previous deployment exposed that old key to compiler child
   processes.

## Rollback

If cache restore or publication is the only problem, set
`COMPILE_CACHE_ENABLED = "false"` in `worker/wrangler.toml` and redeploy the
compiler. Compilation will continue in isolated local workspaces.

Rollback order matters:

1. A TexPal-only rollback is safe.
2. Before rolling back the compiler, roll back TexPal so it stops sending the
   new project identifier. Then redeploy the previous compiler Git revision,
   including its prior Worker configuration and container image. A Worker
   version rollback alone does not restore container configuration or routes.
3. Before rolling the API Worker back to its URL-based version, first restore
   the old public compiler and verify its URL. Keep `LATEX_COMPILER_URL`
   available until the full rollout and rollback window are complete.

Do not make the hardened compiler public as a cache workaround.

## Useful checks

```sh
npm run containers:list
npm run containers:images
npx wrangler deployments list
npx wrangler tail
```

R2 expiration is asynchronous. The application refuses to restore state after
86,400 seconds even if lifecycle deletion has not physically completed yet.
