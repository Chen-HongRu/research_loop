/** Request admission and submission only. Pi owns all native file tools. */
import { connect } from "node:net";
import { isDeepStrictEqual } from "node:util";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { validateToolArguments } from "@earendil-works/pi-ai";

export default function (pi: ExtensionAPI) {
  const binding = JSON.parse(process.env.RL_PI_BINDING!);
  const parameters = binding.schema;
  let failed = false;
  let submitted = false;
  async function bridge(request: Record<string, unknown>): Promise<any> {
    return new Promise((accept, reject) => {
      const socket = connect(binding.socket);
      let data = "";
      socket.setTimeout(Math.max(1, binding.deadline_ms - Date.now()));
      socket.on("connect", () => socket.write(JSON.stringify(request) + "\n"));
      socket.on("data", (chunk) => {
        data += chunk.toString();
        if (data.includes("\n")) {
          socket.end();
          try {
            const reply = JSON.parse(data.slice(0, data.indexOf("\n")));
            if (!reply.ok) reject(new Error(reply.error));
            else accept(reply.result);
          } catch (error) { reject(error); }
        }
      });
      socket.on("timeout", () => socket.destroy(new Error("Invocation deadline reached")));
      socket.on("error", reject);
      socket.on("end", () => { if (!data.includes("\n")) reject(new Error("Incomplete admission reply")); });
    });
  }
  pi.on("cache_warming_decision", () => ({ action: "stop" }));
  pi.on("session_before_compact", () => ({ cancel: true }));
  pi.on("before_provider_request", async (event, ctx) => {
    let payload = event.payload;
    if (ctx.model?.api === "openai-completions" && typeof payload === "object" && payload !== null && !Array.isArray(payload)) {
      const { response_format: _responseFormat, ...request } = payload as Record<string, unknown>;
      payload = request;
    }
    try {
      if (failed || submitted) throw new Error("Invocation finished");
      if (ctx.model?.provider !== binding.provider || ctx.model?.id !== binding.model || pi.getThinkingLevel() !== binding.reasoning) {
        await bridge({ action: "reject", reason: "Provider/model/thinking differs from the exact request" });
        throw new Error("Model identity mismatch");
      }
      if (pi.getSettings().retry?.provider?.maxRetries !== 0) {
        await bridge({ action: "reject", reason: "Provider retries must be disabled" });
        throw new Error("Unsupported provider retry profile");
      }
      if (ctx.model?.api === "openai-codex-responses" && pi.getSettings().transport !== "sse") {
        await bridge({ action: "reject", reason: "Codex transport must be sse" });
        throw new Error("Unsupported Codex transport profile");
      }
      await bridge({ action: "provider_request" });
    } catch {
      failed = true;
      ctx.abort();
    }
    return payload;
  });
  pi.registerTool({
    name: "submit_result", label: "submit_result", description: binding.submit_description,
    parameters, constrainedSampling: false,
    prepareArguments(args) {
      try {
        const validated = validateToolArguments({ name: "submit_result", description: "", parameters }, { type: "toolCall", id: "validation", name: "submit_result", arguments: args });
        if (!isDeepStrictEqual(args, validated)) throw new Error("Submission schema coercion is forbidden");
        return args;
      } catch (error) { failed = true; throw error; }
    },
    async execute(_toolCallId, args) {
      if (failed || submitted) throw new Error("Invocation finished");
      await bridge({ action: "submit", arguments: args });
      submitted = true;
      return { content: [{ type: "text", text: "Result submitted." }], details: args, terminate: true };
    },
  });
}
