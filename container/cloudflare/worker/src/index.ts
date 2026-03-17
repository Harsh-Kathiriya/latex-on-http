import { Container, getContainer } from "@cloudflare/containers";

interface Env {
  LATEX_CONTAINER: DurableObjectNamespace<LatexContainer>;
}

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

const MAX_INSTANCES = 5;
const WARM_STATUSES = new Set(["running", "healthy"]);

interface ContainerLoad {
  index: number;
  stub: DurableObjectStub<LatexContainer>;
  active: number;
  capacity: number;
}

/**
 * Query the Flask /builds/status endpoint on a warm container to get its
 * current compilation load. Returns null if the call fails (container
 * not ready, network issue, etc.) so the caller can treat it as unavailable.
 */
async function getContainerLoad(
  stub: DurableObjectStub<LatexContainer>,
  index: number
): Promise<ContainerLoad | null> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 2000);
    const resp = await stub.fetch(
      new Request("http://container/builds/status", {
        signal: controller.signal,
      })
    );
    clearTimeout(timeout);
    if (!resp.ok) return null;
    const data = (await resp.json()) as { active: number; capacity: number };
    return { index, stub, active: data.active, capacity: data.capacity };
  } catch {
    return null;
  }
}

/**
 * Finds the best container to handle a compilation request.
 *
 * 1. Check getState() on all instances in parallel (lightweight DO storage read).
 * 2. For every warm container, call /builds/status in parallel to get load info.
 * 3. Pick the warm container with the lowest active compilation count that
 *    still has capacity.
 * 4. If all warm containers are at capacity, cold-start the first sleeping one.
 * 5. If everything is full, fall back to the least-loaded warm container
 *    (Gunicorn will queue the request).
 */
async function findAvailableContainer(
  env: Env
): Promise<DurableObjectStub<LatexContainer>> {
  const stubs = Array.from({ length: MAX_INSTANCES }, (_, i) =>
    getContainer(env.LATEX_CONTAINER, `instance-${i}`)
  );

  // Step 1: check which containers are warm (parallel, no Docker start)
  const states = await Promise.all(
    stubs.map((stub) =>
      stub
        .getState()
        .then((s: { status: string }) => s.status)
        .catch(() => "unknown")
    )
  );

  console.log(
    `Container states: ${states.map((s, i) => `instance-${i}=${s}`).join(", ")}`
  );

  const warmIndices = states.reduce<number[]>((acc, s, i) => {
    if (WARM_STATUSES.has(s)) acc.push(i);
    return acc;
  }, []);

  // No warm containers at all — cold-start instance-0
  if (warmIndices.length === 0) {
    console.log("No warm containers, cold-starting instance-0");
    return stubs[0];
  }

  // Step 2: check load on all warm containers in parallel
  const loads = (
    await Promise.all(
      warmIndices.map((i) => getContainerLoad(stubs[i], i))
    )
  ).filter((l): l is ContainerLoad => l !== null);

  // If /status calls all failed, just use the first warm container
  if (loads.length === 0) {
    console.log(
      `Status checks failed, falling back to warm instance-${warmIndices[0]}`
    );
    return stubs[warmIndices[0]];
  }

  console.log(
    `Container loads: ${loads.map((l) => `instance-${l.index}=${l.active}/${l.capacity}`).join(", ")}`
  );

  // Step 3: sort by active count, pick the least loaded with spare capacity
  loads.sort((a, b) => a.active - b.active);
  const available = loads.find((l) => l.active < l.capacity);

  if (available) {
    console.log(
      `Routing to instance-${available.index} (load ${available.active}/${available.capacity})`
    );
    return available.stub;
  }

  // Step 4: all warm containers are at capacity — cold-start a sleeping one
  const sleepingIndex = states.findIndex(
    (s) => !WARM_STATUSES.has(s) && s !== "unknown"
  );

  if (sleepingIndex !== -1) {
    console.log(
      `All warm containers full, cold-starting instance-${sleepingIndex}`
    );
    return stubs[sleepingIndex];
  }

  // Step 5: everything is either full or unknown — use the least loaded warm one
  console.log(
    `All containers busy, queuing on instance-${loads[0].index} (least loaded)`
  );
  return loads[0].stub;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return new Response("ok", { status: 200 });
    }

    const container = await findAvailableContainer(env);
    return container.fetch(request);
  },

  async scheduled(
    _controller: ScheduledController,
    env: Env
  ): Promise<void> {
    const stub = getContainer(env.LATEX_CONTAINER, "instance-0");
    await stub.fetch(new Request("http://container/health"));
  },
};
