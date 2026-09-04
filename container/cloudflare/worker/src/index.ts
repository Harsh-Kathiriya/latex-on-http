import { Container, getContainer } from "@cloudflare/containers";

interface Env {
  LATEX_CONTAINER: DurableObjectNamespace<LatexContainer>;
  AWS_ACCESS_KEY_ID: string;
  AWS_SECRET_ACCESS_KEY: string;
  R2_BUCKET_NAME: string;
  R2_ACCOUNT_ID: string;
  COMPILE_CACHE_ENABLED: string;
}

// Keep the deployed Durable Object identity stable across this rollout so the
// existing warmed instance and the rollback path remain usable.
const CONTAINER_ID = "instance-0";
const MAX_REQUEST_BYTES = 40 * 1024 * 1024;

export class LatexContainer extends Container<Env> {
  defaultPort = 8080;
  sleepAfter = "5m";
  pingEndpoint = "localhost/health";

  // These credentials exist only long enough for the entrypoint to mount R2.
  // The entrypoint removes them before starting Gunicorn or any TeX process.
  override envVars = {
    AWS_ACCESS_KEY_ID: this.env.AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY: this.env.AWS_SECRET_ACCESS_KEY,
    R2_BUCKET_NAME: this.env.R2_BUCKET_NAME,
    R2_ACCOUNT_ID: this.env.R2_ACCOUNT_ID,
    COMPILE_CACHE_ENABLED: this.env.COMPILE_CACHE_ENABLED,
  };

  override onStart(): void {
    console.log("LaTeX container started");
  }

  override onStop(params: { exitCode: number; reason: string }): void {
    console.log(
      `LaTeX container stopped (exit=${params.exitCode}, reason=${params.reason})`,
    );
  }

  override onError(error: unknown): void {
    console.error("LaTeX container error:", error);
  }
}

function compilerContainer(env: Env): DurableObjectStub<LatexContainer> {
  return getContainer(env.LATEX_CONTAINER, CONTAINER_ID);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health" && request.method === "GET") {
      return Response.json({ status: "ok" });
    }

    if (url.pathname !== "/builds/sync") {
      return new Response("Not found", { status: 404 });
    }
    if (request.method !== "POST") {
      return new Response("Method not allowed", {
        status: 405,
        headers: { Allow: "POST" },
      });
    }

    const contentType = request.headers.get("content-type") ?? "";
    if (!contentType.toLowerCase().startsWith("application/json")) {
      return new Response("JSON request required", { status: 415 });
    }

    const contentLength = Number(request.headers.get("content-length"));
    if (Number.isFinite(contentLength) && contentLength > MAX_REQUEST_BYTES) {
      return new Response("Request too large", { status: 413 });
    }

    return compilerContainer(env).fetch(request);
  },

  async scheduled(
    _controller: ScheduledController,
    env: Env,
  ): Promise<void> {
    const response = await compilerContainer(env).fetch(
      new Request("http://container/health"),
    );
    if (!response.ok) {
      console.error(`LaTeX warmup failed with HTTP ${response.status}`);
    }
  },
};
