// A test provider, never evidence of model coding ability or research benefit.
import {
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
  type FauxResponseFactory,
} from "@earendil-works/pi-ai/providers/faux";
import {
  FIXED_SOURCE,
  INITIAL_SOURCE,
  TEST_COMMAND,
  WRONG_SOURCE,
  trace,
} from "./common.ts";

export function syntheticProvider(root: string) {
  const faux = fauxProvider({ models: [{ id: "coding", reasoning: true }] });
  const respond: FauxResponseFactory = async (context, options) => {
    if (process.env.RL_SPIKE_FAULT === "false-success")
      return fauxAssistantMessage(
        JSON.stringify({
          summary: "Unverified success claim",
          tests_passed: true,
        }),
      );
    if (process.env.RL_SPIKE_FAULT === "model") {
      trace(root, "fault.model.waiting");
      await new Promise<void>((resolve) => {
        if (options?.signal?.aborted) resolve();
        else
          options?.signal?.addEventListener("abort", () => resolve(), {
            once: true,
          });
      });
    }
    // Derive the next action from persisted tool feedback, not a local counter.
    const feedback = context.messages.filter(
      (message) => message.role === "toolResult",
    );
    const turn = feedback.length;
    const edit = (oldText: string, newText: string) =>
      fauxToolCall("edit", { path: "app.py", edits: [{ oldText, newText }] });
    const interruptedBash =
      process.env.RL_SPIKE_FAULT === "bash"
        ? "echo effect >> effect.log; sleep 2; " + TEST_COMMAND
        : TEST_COMMAND;
    const actions = [
      fauxToolCall("read", { path: "app.py" }),
      fauxToolCall("read", { path: "test_app.py" }),
      edit(INITIAL_SOURCE, WRONG_SOURCE),
      fauxToolCall("bash", { command: interruptedBash }),
      edit(WRONG_SOURCE, FIXED_SOURCE),
      fauxToolCall("bash", { command: TEST_COMMAND }),
    ];
    if (turn === 4 && !feedback[3]?.isError)
      throw new Error("Synthetic fixture expected real failing test feedback");
    if (turn === 6 && feedback[5]?.isError)
      throw new Error("Synthetic fixture expected real passing test feedback");
    trace(root, "synthetic.feedback", {
      turn,
      previousToolFailed: feedback.at(-1)?.isError ?? false,
    });
    return turn < actions.length
      ? fauxAssistantMessage(actions[turn], { stopReason: "toolUse" })
      : fauxAssistantMessage(
          JSON.stringify({
            summary: "Corrected reversed bounds using actual test feedback",
            tests_passed: true,
          }),
        );
  };
  faux.setResponses(Array.from({ length: 40 }, () => respond));
  return faux;
}
