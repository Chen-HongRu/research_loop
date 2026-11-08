import asyncio
import json
from types import SimpleNamespace

import pytest
from agents import Agent, GuardrailFunctionOutput, function_tool, output_guardrail
from pydantic import BaseModel
from research_loop import codex
from research_loop.workflow import FatalResearchError


class Answer(BaseModel):
    count: int


class FakeTurn:
    id = "turn-synthetic"

    def __init__(self, runtime, thread):
        self.runtime, self.thread = runtime, thread
        self.interrupted = asyncio.Event()
        self.terminal = False

    async def interrupt(self):
        if self.terminal:
            raise codex.InvalidRequestError(-32600, "no active turn to interrupt")
        self.runtime.interrupts += 1
        self.interrupted.set()

    async def stream(self):
        script = self.runtime.scripts.pop(0)
        queue = self.runtime.events

        async def execute():
            try:
                await script(self.runtime, self.thread, self)
            finally:
                await queue.put(None)

        task = asyncio.create_task(execute())
        while True:
            event = await queue.get()
            if event is None:
                await task
                break
            yield event
        yield SimpleNamespace(
            method="item/completed",
            payload={
                "item": {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Completed the stage.",
                }
            },
        )
        self.terminal = True
        yield SimpleNamespace(
            method="turn/completed", payload={"turn": {"status": "completed"}}
        )


class FakeThread:
    def __init__(self, runtime, identifier):
        self.runtime, self.id = runtime, identifier

    async def turn(self, prompt, **options):
        self.runtime.turns.append((self.id, prompt, options))
        return FakeTurn(self.runtime, self)


class FakeRuntime:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self._client = self
        self.started, self.turns, self.replies = [], [], []
        self.resumed = []
        self.interrupts = 0
        self.events = asyncio.Queue()
        self.runner = None
        self.prefix = "thread"

    async def thread_start(self, params):
        self.started.append(params)
        return SimpleNamespace(
            model=codex.MODEL,
            reasoning_effort=codex.EFFORT,
            thread=SimpleNamespace(id=f"{self.prefix}-{len(self.started)}"),
        )

    async def _start_turn(self, thread_id, input, params, for_handle):
        self.turns.append((thread_id, input[0]["text"], params))
        return SimpleNamespace(turn=SimpleNamespace(id="turn-synthetic")), FakeTurn(
            self, SimpleNamespace(id=thread_id)
        )

    async def thread_resume(self, thread_id, params):
        self.resumed.append((thread_id, params))
        return SimpleNamespace(
            model=codex.MODEL,
            reasoning_effort=codex.EFFORT,
            thread=SimpleNamespace(id=thread_id),
        )

    async def close(self):
        pass

    async def call(self, thread, tool, args):
        call_id = f"call-{len(self.replies)}"
        reply = await self.runner._tool(
            {
                "threadId": thread.id,
                "turnId": "turn-synthetic",
                "callId": call_id,
                "tool": tool,
                "arguments": args,
            }
        )
        self.replies.append(reply)
        await self.events.put(
            SimpleNamespace(
                method="item/completed",
                payload={
                    "item": {
                        "type": "dynamicToolCall",
                        "id": call_id,
                        "success": reply["success"],
                    }
                },
            )
        )
        return reply


def prepared(tmp_path, monkeypatch, scripts, **options):
    runtime = FakeRuntime(scripts)
    monkeypatch.setattr(codex, "AsyncThread", FakeThread)
    monkeypatch.setattr(
        codex,
        "AsyncTurnHandle",
        lambda client, thread_id, turn_id, *, _subscription: _subscription,
    )
    runner = codex.CodexRoleRunner(
        event_path=tmp_path / "events.jsonl",
        allow_native_response_budget=True,
        **options,
    )
    runner._client = runtime
    runner._loop = asyncio.get_running_loop()
    runner._cwd = tmp_path
    runner._stream = runner.event_path.open("x")
    runtime.runner = runner
    return runner, runtime


def test_schema_guardrail_and_validation_correction(tmp_path, monkeypatch):
    async def scenario():
        checks = []

        @output_guardrail
        def positive(context, agent, output):
            return GuardrailFunctionOutput("count must be positive", output.count <= 0)

        async def script(runtime, thread, turn):
            assert not (await runtime.call(thread, "submit_plan", {"count": "bad"}))[
                "success"
            ]
            assert not (await runtime.call(thread, "submit_plan", {"count": 0}))[
                "success"
            ]
            assert not (await runtime.call(thread, "submit_plan", {"count": 1}))[
                "success"
            ]
            assert (await runtime.call(thread, "submit_plan", {"count": 2}))["success"]

        def validate(output):
            checks.append(output.count)
            if output.count == 1:
                raise ValueError("count must reference observed evidence")

        runner, runtime = prepared(tmp_path, monkeypatch, [script])
        agent = Agent(name="Plan", output_type=Answer, output_guardrails=[positive])
        result = await runner.run(agent, "plan", max_turns=4096, validate=validate)
        assert result.final_output == Answer(count=2)
        assert result.summary == "Completed the stage."
        assert checks == [1, 2]
        start = runtime.started[0]
        assert start["model"] == "gpt-6.1-sol"
        assert start["allowModelFallback"] is False
        assert start["config"]["model_reasoning_effort"] == "medium"
        assert start["environments"] == []
        assert start["permissions"] == "study"
        assert "sandbox" not in start
        assert runtime.turns[0][2] == {
            "model": "gpt-6.1-sol",
            "effort": "medium",
            "environments": [],
        }
        events = [
            json.loads(line) for line in runner.event_path.read_text().splitlines()
        ]
        assert events[0]["model_response_count"] is None
        runner._stream.close()

    asyncio.run(scenario())


def test_read_tool_seal_duplicates_and_allowed_tools(tmp_path, monkeypatch):
    async def scenario():
        reads, validations = [], []

        @function_tool(failure_error_function=None)
        async def read_note(context: codex.ToolContext, name: str):
            assert context.tool_name == "read_note"
            assert context.tool_call_id.startswith("call-")
            reads.append(name)
            return "synthetic evidence"

        async def script(runtime, thread, turn):
            assert not (await runtime.call(thread, "shell", {"cmd": "anything"}))[
                "success"
            ]
            assert (await runtime.call(thread, "read_note", {"name": "note"}))[
                "success"
            ]
            first = await runtime.call(thread, "submit_execute", {"output": "done"})
            assert first["success"]
            assert (
                await runtime.call(thread, "submit_execute", {"output": "done"})
                == first
            )
            assert not (
                await runtime.call(thread, "submit_execute", {"output": "overwrite"})
            )["success"]
            assert not (await runtime.call(thread, "read_note", {"name": "again"}))[
                "success"
            ]

        runner, runtime = prepared(tmp_path, monkeypatch, [script])
        agent = Agent(name="Execute", tools=[read_note])
        result = await runner.run(
            agent, "execute", validate=lambda output: validations.append(output)
        )
        assert result.final_output == "done"
        assert reads == ["note"]
        assert validations == ["done"]
        assert [t["name"] for t in runtime.started[0]["dynamicTools"]] == [
            "read_note",
            "submit_execute",
        ]
        runner._stream.close()

    asyncio.run(scenario())


def test_native_thread_continuation_does_not_replay_tools(tmp_path, monkeypatch):
    async def scenario():
        calls = []

        @function_tool(failure_error_function=None)
        def observe():
            calls.append("observation")
            return "seen"

        async def first(runtime, thread, turn):
            await runtime.call(thread, "observe", {})

        async def second(runtime, thread, turn):
            await runtime.call(thread, "submit_plan", {"count": 3})

        runner, runtime = prepared(tmp_path, monkeypatch, [first, second])
        agent = Agent(name="Plan", output_type=Answer, tools=[observe])
        with pytest.raises(codex.CodexRoleError) as failed:
            await runner.run(agent, "first")
        history = failed.value.run_data.history
        assert history == [{"type": codex.REFERENCE, "thread_id": "thread-1"}]
        history.append({"role": "user", "content": "Submit the observed result"})
        result = await runner.run(agent, history)
        assert result.final_output.count == 3
        assert result.to_input_list() == [history[0]]
        assert len(runtime.started) == 1
        assert calls == ["observation"]
        assert "observation" not in runtime.turns[1][1]
        runner._stream.close()

    asyncio.run(scenario())


def test_fatal_tool_error_interrupts_and_propagates(tmp_path, monkeypatch):
    async def scenario():
        @function_tool(failure_error_function=None)
        async def compute():
            raise FatalResearchError("unknown synthetic compute state")

        async def script(runtime, thread, turn):
            reply = await runtime.call(thread, "compute", {})
            assert not reply["success"]
            await turn.interrupted.wait()

        runner, runtime = prepared(tmp_path, monkeypatch, [script])
        with pytest.raises(FatalResearchError, match="unknown synthetic"):
            await runner.run(Agent(name="Execute", tools=[compute]), "go")
        assert runtime.interrupts >= 1
        assert not runner._roles["thread-1"].tasks
        runner._stream.close()

    asyncio.run(scenario())


def test_cancellation_quiesces_tool_and_turn(tmp_path, monkeypatch):
    async def scenario():
        entered, cleaned = asyncio.Event(), asyncio.Event()

        @function_tool(failure_error_function=None)
        async def compute():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        async def script(runtime, thread, turn):
            # Simulate SDK's independent request-dispatch thread/task.
            task = asyncio.create_task(runtime.call(thread, "compute", {}))
            await asyncio.gather(task, return_exceptions=True)
            await turn.interrupted.wait()

        runner, runtime = prepared(tmp_path, monkeypatch, [script])
        task = asyncio.create_task(
            runner.run(Agent(name="Execute", tools=[compute]), "go")
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
        assert runtime.interrupts >= 1
        assert not runner._roles["thread-1"].tasks
        runner._stream.close()

    asyncio.run(scenario())


def test_summary_failure_retains_accepted_output(tmp_path, monkeypatch):
    async def scenario():
        publications = []

        async def script(runtime, thread, turn):
            await runtime.call(thread, "submit_manager", {"output": "published"})
            raise RuntimeError("synthetic summary transport failure")

        runner, _runtime = prepared(tmp_path, monkeypatch, [script])
        result = await runner.run(
            Agent(name="Manager"), "go", validate=publications.append
        )
        assert result.final_output == "published"
        assert result.summary_missing
        assert publications == ["published"]
        runner._stream.close()

    asyncio.run(scenario())


def test_response_budget_is_not_silently_reinterpreted(tmp_path):
    runner = codex.CodexRoleRunner(event_path=tmp_path / "events.jsonl")
    with pytest.raises(codex.CodexRoleError, match="model responses"):
        asyncio.run(runner.run(Agent(name="Plan"), "go", max_turns=4096))


def test_unknown_submission_callback_effects_are_fatal(tmp_path, monkeypatch):
    async def scenario():
        async def script(runtime, thread, turn):
            assert not (
                await runtime.call(thread, "submit_manager", {"output": "draft"})
            )["success"]
            await turn.interrupted.wait()

        def publish(output):
            raise OSError("synthetic publication failure")

        runner, _ = prepared(tmp_path, monkeypatch, [script])
        with pytest.raises(OSError, match="synthetic publication"):
            await runner.run(Agent(name="Manager"), "go", validate=publish)
        runner._stream.close()

    asyncio.run(scenario())


def test_side_effecting_value_error_is_returned_to_controller_once(
    tmp_path, monkeypatch
):
    async def scenario():
        calls = []

        def publish(output):
            calls.append(output)
            raise ValueError("synthetic publication failure")

        publish.codex_side_effecting = True

        async def script(runtime, thread, turn):
            await runtime.call(thread, "submit_manager", {"output": "draft"})
            assert not (
                await runtime.call(thread, "submit_manager", {"output": "draft"})
            )["success"]
            await turn.interrupted.wait()

        runner, _ = prepared(tmp_path, monkeypatch, [script])
        with pytest.raises(ValueError, match="synthetic publication"):
            await runner.run(Agent(name="Manager"), "go", validate=publish)
        assert calls == ["draft"]
        runner._stream.close()

    asyncio.run(scenario())


def test_cancel_during_turn_start_retains_and_stops_handle(tmp_path, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def script(runtime, thread, turn):
            await turn.interrupted.wait()

        runner, runtime = prepared(tmp_path, monkeypatch, [script])
        original = runtime._start_turn

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        runtime._start_turn = delayed
        task = asyncio.create_task(runner.run(Agent(name="Execute"), "go"))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.interrupts >= 1
        runner._stream.close()

    asyncio.run(scenario())


def test_independent_role_clients_do_not_block_on_other_tool(tmp_path, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        @function_tool(failure_error_function=None)
        async def slow_read():
            entered.set()
            await release.wait()
            return "read"

        async def slow(runtime, thread, turn):
            await runtime.call(thread, "slow_read", {})
            await runtime.call(thread, "submit_slow", {"output": "done"})

        async def fast(runtime, thread, turn):
            await runtime.call(thread, "submit_fast", {"output": "independent"})

        runner, _ = prepared(tmp_path, monkeypatch, [])
        clients = []

        async def new_client():
            runtime = FakeRuntime([slow if not clients else fast])
            runtime.prefix = f"client{len(clients)}"
            runtime.runner = runner
            clients.append(runtime)
            return runtime

        monkeypatch.setattr(runner, "_new_client", new_client)
        pending = asyncio.create_task(
            runner.run(Agent(name="Slow", tools=[slow_read]), "slow")
        )
        await entered.wait()
        result = await asyncio.wait_for(
            runner.run(Agent(name="Fast"), "fast"), timeout=1
        )
        assert result.final_output == "independent"
        assert not pending.done()
        assert len(clients) == 2
        release.set()
        await pending
        runner._stream.close()

    asyncio.run(scenario())


def test_ordinary_tool_failure_resumes_without_replaying_success(tmp_path, monkeypatch):
    async def scenario():
        observed, attempts = [], []
        condition = False

        @function_tool(failure_error_function=None)
        def read_once():
            observed.append("read")
            return "observed"

        @function_tool(failure_error_function=None)
        def compute():
            attempts.append(condition)
            if not condition:
                raise ValueError("explicitly not dispatched")
            return "completed"

        async def first(runtime, thread, turn):
            await runtime.call(thread, "read_once", {})
            await runtime.call(thread, "compute", {})
            await turn.interrupted.wait()

        async def second(runtime, thread, turn):
            await runtime.call(thread, "compute", {})
            await runtime.call(thread, "submit_execute", {"output": "done"})

        runner, runtime = prepared(tmp_path, monkeypatch, [first, second])
        agent = Agent(name="Execute", tools=[read_once, compute])
        with pytest.raises(ValueError, match="not dispatched") as failure:
            await runner.run(agent, "first")
        condition = True
        history = failure.value.run_data.history + [
            {
                "role": "user",
                "content": "The condition changed; execute the remaining request.",
            }
        ]
        result = await runner.run(agent, history)
        assert result.final_output == "done"
        assert observed == ["read"]
        assert attempts == [False, True]
        assert len(runtime.started) == 1
        runner._stream.close()

    asyncio.run(scenario())


def test_durable_controlled_resume_uses_native_thread_and_binding(
    tmp_path, monkeypatch
):
    async def scenario():
        state = tmp_path / "state"
        state.mkdir()
        bindings = state / "roles"
        bindings.mkdir()

        async def first(runtime, thread, turn):
            pass

        async def second(runtime, thread, turn):
            await runtime.call(thread, "submit_plan", {"count": 7})

        agent = Agent(name="Plan", instructions="same contract", output_type=Answer)
        first_path = tmp_path / "first"
        first_path.mkdir()
        original, before = prepared(first_path, monkeypatch, [first], state_dir=state)
        original._config, original._bindings = object(), bindings

        async def client_before():
            original._clients.append(before)
            return before

        monkeypatch.setattr(original, "_new_client", client_before)
        with pytest.raises(codex.CodexRoleError) as failure:
            await original.run(agent, "original input")
        original._stream.close()
        later_path = tmp_path / "later"
        later_path.mkdir()
        resumed, after = prepared(later_path, monkeypatch, [second], state_dir=state)
        resumed._config, resumed._bindings = object(), bindings

        async def client_after():
            resumed._clients.append(after)
            return after

        monkeypatch.setattr(resumed, "_new_client", client_after)
        result = await resumed.run(
            agent,
            failure.value.run_data.history
            + [{"role": "user", "content": "Submit the pending result"}],
        )
        assert result.final_output == Answer(count=7)
        assert not after.started
        assert after.resumed[0][0] == "thread-1"
        assert after.turns[0][2]["environments"] == []
        assert json.loads((bindings / "thread-1.json").read_text())["sealed"] is True
        resumed._stream.close()

    asyncio.run(scenario())
