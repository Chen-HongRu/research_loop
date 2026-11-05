import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { createModels } from "@earendil-works/pi-ai/models";
import {
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
  fauxText,
  type FauxResponseFactory,
} from "@earendil-works/pi-ai/providers/faux";
export function events(root: string) {
  try {
    return readFileSync(join(root, "events.jsonl"), "utf8")
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line));
  } catch {
    return [];
  }
}
async function until(predicate: () => boolean) {
  const end = Date.now() + 15_000;
  while (!predicate()) {
    assert.ok(
      Date.now() < end,
      "Timed out waiting for durable Manager publication",
    );
    await delay(10);
  }
}

export function scripted(
  root: string,
  failManager:
    | boolean
    | "duplicate"
    | "mixed-error"
    | "symlink"
    | "hang"
    | "premature" = false,
) {
  const faux = fauxProvider({ models: [{ id: "workflow", reasoning: false }] });
  const models = createModels();
  models.setProvider(faux.provider);
  let firstRead = false;
  let managerWaiting = false;
  const observedNotes: string[] = [];
  const respond: FauxResponseFactory = async (ctx, options) => {
    const first = ctx.messages.find((message) => message.role === "user");
    const text =
      first?.role === "user"
        ? typeof first.content === "string"
          ? first.content
          : first.content
              .map((part) => (part.type === "text" ? part.text : ""))
              .join("")
        : "";
    const feedback = ctx.messages.filter(
      (message) => message.role === "toolResult",
    );
    const turn = feedback.length;
    const call = (name: string, args: Parameters<typeof fauxToolCall>[1]) =>
      fauxAssistantMessage(fauxToolCall(name, args), { stopReason: "toolUse" });
    if (text.startsWith("Read the latest published knowledge")) {
      if (failManager === "hang") {
        managerWaiting = true;
        await new Promise<void>((resolve) => {
          if (options?.signal?.aborted) resolve();
          else
            options?.signal?.addEventListener("abort", () => resolve(), {
              once: true,
            });
        });
        return fauxAssistantMessage("aborted", { stopReason: "aborted" });
      }
      if (failManager === true)
        return fauxAssistantMessage(
          "Manager intentionally failed before editing/publishing",
        );
      if (turn === 0) return call("read", { path: "research.md" });
      if (turn === 1 && failManager === "symlink")
        return call("bash", {
          command:
            "rm research.md && ln -s '" +
            join(root, "synthetic-secret").replaceAll("'", "'\\''") +
            "' research.md",
        });
      if (turn === 1)
        return call("write", {
          path: "research.md",
          content:
            "# Reviewed knowledge\n\nRecorded CPU evidence and limitations; development feedback is adaptive.\n",
        });
      if (
        turn === 2 &&
        failManager === false &&
        text.includes('"id":"r0001"') &&
        !text.includes('"id":"r0002"')
      )
        await until(() => firstRead);
      if (turn === 2 && failManager === "duplicate")
        return fauxAssistantMessage(
          [
            fauxToolCall("publish_notes", {}),
            fauxToolCall("publish_notes", {}),
          ],
          { stopReason: "toolUse" },
        );
      if (turn === 2 && failManager === "mixed-error")
        return fauxAssistantMessage(
          [
            fauxToolCall("publish_notes", {}),
            fauxToolCall("read", { path: "research.md" }),
          ],
          { stopReason: "toolUse" },
        );
      if (turn >= 4 && failManager === "mixed-error")
        return fauxAssistantMessage(
          "Intentional Manager failure after mixed finish/read tools",
          { stopReason: "error", errorMessage: "synthetic tail failure" },
        );
      if (turn >= 3 && failManager === "symlink")
        return fauxAssistantMessage("Cannot publish an untrusted draft link");
      return call("publish_notes", {});
    }
    if (text.startsWith("You are an independent fresh Review")) {
      assert.equal(
        JSON.stringify(ctx.messages).includes(
          "SYNTHETIC_PRIVATE_EXECUTE_DISCUSSION",
        ),
        false,
        "Review must not inherit hidden Execute chat",
      );
      if (turn === 0) {
        const receipts = JSON.parse(
          text.split("\nTrusted receipts: ")[1].split("\nFixed evaluation:")[0],
        );
        return call("read", { path: receipts[0].rawResult });
      }
      const cited = JSON.parse(
        text
          .split("\nPrior independently reviewed records: ")[1]
          .split("\nTrusted receipts:")[0],
      );
      if (turn === 1 && cited.length)
        return call("read", { path: cited[0].receipts[0].rawResult });
      if (turn === 2 && cited.length)
        assert.equal(
          feedback.at(-1)?.isError,
          false,
          "Independent R2 Review must read the cited original R1 evidence",
        );
      return call("submit_review", {
        interpretation:
          "Measured the actual frozen candidate; no improvement claim",
        limitations: ["Small synthetic development set", "No final-test claim"],
        knowledgeChanged: true,
        goal:
          failManager === "premature" && turn === 1 && !cited.length
            ? "achieved"
            : "not-achieved",
        continue: true,
        nextQuestion:
          "Probe a small offset and compare the prior measured result",
      });
    }
    if (text.includes("\nImmutable human constraints:")) {
      const second = text.includes('"basedOn":["r0001"]');
      if (turn === 0)
        return fauxAssistantMessage(
          [
            fauxText("SYNTHETIC_PRIVATE_EXECUTE_DISCUSSION"),
            fauxToolCall("read", { path: "candidate.py" }),
          ],
          { stopReason: "toolUse" },
        );
      if (turn === 1)
        return call("write", {
          path: "candidate.py",
          content: `def fit_predict(train, dev):\n    mean = sum(row['target'] for row in train) / len(train)\n    return [{'id': row['id'], 'prediction': mean + ${second ? "0.1" : "0.0"}} for row in dev]\n`,
        });
      if (turn === 2)
        return call("bash", {
          command:
            "uv run --no-project --offline python -m py_compile candidate.py",
        });
      if (turn === 3) return call("run_experiment", {});
      return call("finish_execute", {
        summary:
          "Changed candidate and ran actual syntax/test and trusted CPU experiment; limitations retained",
      });
    }
    const second = text.includes('"id":"r0001"');
    if (second && !failManager) {
      if (turn === 0)
        return call("read", { path: join(root, "knowledge", "research.md") });
      if (turn === 1) {
        observedNotes.push(JSON.stringify(feedback.at(-1)?.content));
        firstRead = true;
        await until(() =>
          events(root).some(
            (row) =>
              row.event === "knowledge.published" ||
              row.event === "knowledge.recovered",
          ),
        );
        return call("read", { path: join(root, "knowledge", "research.md") });
      }
      if (turn === 2)
        observedNotes.push(JSON.stringify(feedback.at(-1)?.content));
    } else if (turn === 0) return call("read", { path: "candidate.py" });
    return call("submit_plan", {
      question: second
        ? "Use R1's measured limitation to test a small offset"
        : "Measure the baseline code path",
      change: second
        ? "Add 0.1 offset based on R1"
        : "Write an explicit zero-offset baseline",
      rationale: second
        ? "R1 supplies actual baseline feedback and small-data limitation"
        : "Establish the measured reference",
      basedOn: second ? ["r0001"] : [],
    });
  };
  faux.setResponses(Array.from({ length: 100 }, () => respond));
  return { models, faux, observedNotes, managerWaiting: () => managerWaiting };
}
