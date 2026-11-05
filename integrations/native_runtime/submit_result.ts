/** Frozen native tool bindings; Python retains capabilities and authorization. */
import { readFileSync } from "node:fs";
import { connect } from "node:net";
import { resolve } from "node:path";
import { isDeepStrictEqual } from "node:util";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { validateToolArguments } from "@earendil-works/pi-ai";

export default function (pi: ExtensionAPI) {
  const parameters = JSON.parse(readFileSync(resolve(process.cwd(), "response-schema.json"), "utf8"));
  const binding = JSON.parse(readFileSync(resolve(process.cwd(), "tool-binding.json"), "utf8"));
  let failed = false;
  let submitted = false;
  const allowed = new Set(["submit_result", ...Object.keys(binding.tools)]);
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
      socket.on("end", () => { if (!data.includes("\n")) reject(new Error("Incomplete native bridge reply")); });
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
      if (pi.getSettings().retry?.provider?.maxRetries !== 0) {
        await bridge({ action: "reject", reason: "Set Pi retry.provider.maxRetries to 0; internal HTTP retries bypass request accounting." });
        throw new Error("Unsupported provider retry profile");
      }
      if (ctx.model?.api === "openai-codex-responses" && pi.getSettings().transport !== "sse") {
        await bridge({ action: "reject", reason: "Set Pi transport to sse for Codex; WebSocket fallback bypasses request accounting." });
        throw new Error("Unsupported Codex transport profile");
      }
      await bridge({ action: "provider_request", payload });
    } catch {
      failed = true;
      // Pi swallows hook exceptions. Abort its native signal before HTTP admission.
      ctx.abort();
    }
    return payload;
  });
  pi.on("message_end", async (event, ctx) => {
    if (event.message.role !== "assistant") return;
    const calls = event.message.content.filter((block: any) => block.type === "toolCall");
    if (calls.some((call: any) => !allowed.has(call.name)) || (calls.some((call: any) => call.name === "submit_result") && calls.length !== 1)) {
      failed = true;
      try { await bridge({ action: "reject", reason: "Unauthorized or mixed terminal tool call" }); } catch {}
      ctx.abort();
    }
  });
  pi.on("tool_call", async (event, ctx) => {
    if (failed || submitted || !allowed.has(event.toolName)) {
      failed = true;
      ctx.abort();
      return { block: true, terminate: true, reason: "Tool outside the active invocation authorization." };
    }
  });
  pi.on("tool_result", async (event, ctx) => {
    if (event.isError) { failed = true; ctx.abort(); }
  });

  function register(name: string, description: string, schema: any) {
    pi.registerTool({
      name, label: name, description, parameters: schema, constrainedSampling: false,
      prepareArguments(args) {
        try {
          const validated = validateToolArguments({ name, description: "", parameters: schema }, { type: "toolCall", id: "validation", name, arguments: args });
          if (!isDeepStrictEqual(args, validated)) throw new Error("Coerced arguments");
        } catch {
          failed = true;
          throw new Error("Native tool arguments must match their complete schema without coercion.");
        }
        return args;
      },
      async execute(toolCallId, args) {
        if (failed || submitted) throw new Error("Invocation finished");
        try {
          if (name === "submit_result") {
            await bridge({ action: "submit", id: toolCallId, arguments: args });
            submitted = true;
            return { content: [{ type: "text", text: "Result submitted." }], details: args, terminate: true };
          }
          const result = await bridge({ action: "tool", id: toolCallId, name, arguments: args });
          return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
        } catch (error) { failed = true; throw error; }
      },
    });
  }
  for (const [name, tool] of Object.entries(binding.tools) as [string, any][]) register(name, tool.description, tool.parameters);
  register("submit_result", binding.submit_description, parameters);
}
