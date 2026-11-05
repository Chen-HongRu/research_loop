/** Trusted, side-effect-free final output for one native runtime invocation. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { isDeepStrictEqual } from "node:util";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { validateToolArguments } from "@earendil-works/pi-ai";

export default function (pi: ExtensionAPI) {
  // Python publishes the complete envelope schema in this private call's cwd.
  // Keep its root intact so local $refs continue to resolve against its $defs.
  const parameters = JSON.parse(
    readFileSync(resolve(process.cwd(), "response-schema.json"), "utf8"),
  );
  let requested = false;
  pi.on("before_provider_request", (event, ctx) => {
    if (requested) {
      // Hook exceptions are logged and swallowed by Pi. Abort the native signal
      // instead, before a validation failure can trigger another model request.
      ctx.abort();
    }
    requested = true;
    if (
      ctx.model?.api === "openai-completions" &&
      typeof event.payload === "object" &&
      event.payload !== null &&
      !Array.isArray(event.payload)
    ) {
      const { response_format: _responseFormat, ...payload } = event.payload as Record<
        string,
        unknown
      >;
      return payload;
    }
  });

  pi.registerTool({
    name: "submit_result",
    label: "Submit result",
    description:
      "Submit the complete final response envelope as your only tool call. " +
      "The arguments must match the supplied response schema exactly.",
    parameters,
    constrainedSampling: false,
    prepareArguments(args) {
      // Pi accepts plain JSON Schema, including $defs/$refs, but its normal
      // validation coerces values and prints the entire input on failure.
      // Reject coercion and replace that unbounded diagnostic before execution.
      try {
        const validated = validateToolArguments(
          { name: "submit_result", description: "", parameters },
          {
            type: "toolCall",
            id: "validation",
            name: "submit_result",
            arguments: args,
          },
        );
        if (!isDeepStrictEqual(args, validated)) {
          throw new Error("Coerced arguments");
        }
      } catch {
        throw new Error(
          "submit_result arguments must match the complete response schema without coercion.",
        );
      }
      return args;
    },
    async execute(_toolCallId, envelope) {
      return {
        content: [{ type: "text", text: "Result submitted." }],
        details: envelope,
        terminate: true,
      };
    },
  });
}
