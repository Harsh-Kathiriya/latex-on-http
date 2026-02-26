import { Container, getRandom } from "@cloudflare/containers";

/**
 * Env bindings declared in wrangler.toml.
 * LATEX_CONTAINER is a Durable Object namespace that manages container instances.
 */
interface Env {
  LATEX_CONTAINER: DurableObjectNamespace<LatexContainer>;
}

/**
 * Container class that Cloudflare instantiates alongside a Durable Object.
 * Each instance runs our Docker image (gunicorn + Flask on port 8080).
 *
 * - defaultPort: the port inside the container that receives proxied requests.
 * - sleepAfter: idle timeout before Cloudflare shuts the container down.
 *   Shorter = cheaper (you only pay while running), longer = fewer cold starts.
 */
export class LatexContainer extends Container {
  defaultPort = 8080;
  sleepAfter = "5m";
  pingEndpoint = "localhost/";

  override onStart(): void {
    console.log("LaTeX container started");
  }

  override onStop(params: { exitCode: number; reason: string }): void {
    console.log(
      `LaTeX container stopped (exit=${params.exitCode}, reason=${params.reason})`
    );
  }

  override onError(error: unknown): void {
    console.error("LaTeX container error:", error);
  }
}

/**
 * How many container instances to load-balance across.
 * getRandom picks one at random for each request.
 * Increase for higher throughput; each instance runs 2 gunicorn workers.
 * Matches max_instances in wrangler.toml for full utilization.
 */
const INSTANCE_COUNT = 5;

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return new Response("ok", { status: 200 });
    }

    const container = await getRandom(env.LATEX_CONTAINER, INSTANCE_COUNT);
    return container.fetch(request);
  },
};
