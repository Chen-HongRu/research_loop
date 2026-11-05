import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { checkDeadline, trace, type RunConfig } from "./common.ts";
import { join } from "node:path";
import { syntheticProvider } from "./synthetic.ts";

export async function createRuntime(
  root: string,
  config: RunConfig,
): Promise<ModelRuntime> {
  const runtime = await ModelRuntime.create(
    config.provider === "faux"
      ? {
          authPath: join(root, "empty-auth.json"),
          modelsPath: null,
          refreshOnCreate: false,
          allowModelNetwork: false,
        }
      : { allowModelNetwork: false },
  );
  if (config.provider === "faux")
    runtime.registerNativeProvider(syntheticProvider(root).provider);
  if (!runtime.getPhysicalModel(config.provider, config.model))
    throw new Error("Exact requested provider/model unavailable; no fallback.");
  const stream = runtime.streamSimple.bind(runtime);
  runtime.streamSimple = (model, context, options) => {
    checkDeadline(config);
    trace(root, "model.request", { provider: model.provider, model: model.id });
    return stream(model, context, options);
  };
  return runtime;
}
