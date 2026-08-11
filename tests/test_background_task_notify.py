"""core/agent.py — S51 Part D: Part A's live verification found the
completed-task notify-later path was a single-shot injection with no retry.
push_ephemeral() only surfaces a note for exactly one turn, and the old code
discarded the task_id from self._background_task_ids the instant its
task_queue status flipped to success/error -- regardless of whether the
model's response for that turn actually mentioned it. Live testing showed
this is the COMMON case, not an edge case: 2/2 real dispatch-then-unrelated-
follow-up runs silently dropped the notification and Lumina's own
"I'll let you know when it's done" went unfulfilled.

Fix: don't discard on terminal status alone. Track per-task retry attempts
in self._background_task_notifications, keep re-injecting the note each
turn until either the model's prior response demonstrably referenced it (a
crude substring check, not sophisticated content-matching) or
BACKGROUND_TASK_NOTIFY_RETRIES is exhausted -- at which point give up
automatically surfacing it, but task_queue's own result (a separate
lifecycle, RESULT_TTL_SECONDS) is left untouched.
"""
import types
import time
from core import task_queue
from core.agent import LuminaAgent, BACKGROUND_TASK_NOTIFY_RETRIES


class _FakeLLM:
    """No tool calls -- every turn goes straight through to _stream_final()."""
    def __init__(self, response_text="ok"):
        self.response_text = response_text

    def chat(self, messages, tools=None, max_tokens=None):
        return {"content": self.response_text}

    def extract_message(self, response):
        return {"role": "assistant", "content": response["content"]}

    def is_tool_call(self, message):
        return False

    def chat_stream(self, messages, max_tokens):
        yield self.response_text


def _fake_agent(response_text="unrelated answer", background_task_ids=None):
    history = []

    def add_user(content, source="OWNER_DIRECT"):
        history.append({"role": "user", "content": content})

    def add_assistant(content):
        history.append({"role": "assistant", "content": content})

    ctx = types.SimpleNamespace(
        history=history,
        add_user=add_user,
        add_assistant=add_assistant,
        push_ephemeral=lambda block: None,
        build_messages=lambda tool_budget=0, chat_id=None: [],
    )

    ns = types.SimpleNamespace(
        llm=_FakeLLM(response_text),
        ctx=ctx,
        registry=types.SimpleNamespace(
            schema_token_estimate=lambda: 0,
            get_schemas=lambda: [],
            list_enabled=lambda: [],
        ),
        on_tool_call=lambda name, args: None,
        on_tool_result=lambda name, result: None,
        on_think_start=lambda step: None,
        on_think_token=lambda tok: None,
        on_think_end=lambda: None,
        on_response_token=lambda tok: None,
        tts=None,
        owner=True,
        _background_task_ids=set(background_task_ids or []),
        _background_task_notifications={},
        _session_tool_calls=0,
        _skill_nudge_sent=False,
    )
    # chat() calls self._stream_final(...) as a bound method -- a bare
    # SimpleNamespace has no such attribute, so bind the real unbound
    # implementation onto this fake instance.
    ns._stream_final = types.MethodType(LuminaAgent._stream_final, ns)
    return ns


def _fake_result(status="success", result="42"):
    return {"status": status, "result": result, "completed_at": time.time()}


def test_unreferenced_note_survives_one_unrelated_follow_up(monkeypatch):
    """Reproduces the exact 2/2 live-observed failure pattern: dispatch,
    complete, one unrelated follow-up turn. The note must still be tracked
    afterward -- not silently discarded like the old unconditional-discard
    behavior."""
    monkeypatch.setattr(task_queue, "get_task_result", lambda tid: _fake_result())
    fake_self = _fake_agent(response_text="Mars has a thin CO2 atmosphere.",
                             background_task_ids={"tid1"})

    result = LuminaAgent.chat(fake_self, "what's the weather on mars?")

    assert "Mars" in result
    assert "tid1" in fake_self._background_task_ids, "note was discarded after a single unreferenced turn"
    assert fake_self._background_task_notifications["tid1"]["attempts"] == 1


def test_retry_budget_actually_terminates(monkeypatch):
    """Keeps re-injecting for BACKGROUND_TASK_NOTIFY_RETRIES turns, then
    gives up -- doesn't re-inject forever."""
    monkeypatch.setattr(task_queue, "get_task_result", lambda tid: _fake_result())
    fake_self = _fake_agent(response_text="unrelated answer, never mentions it",
                             background_task_ids={"tid1"})

    for i in range(BACKGROUND_TASK_NOTIFY_RETRIES):
        LuminaAgent.chat(fake_self, f"unrelated question {i}")
        assert "tid1" in fake_self._background_task_ids, f"discarded too early, before turn {i+1}"

    # One more turn past the retry budget -- Step 1 should now give up.
    LuminaAgent.chat(fake_self, "one more unrelated question")
    assert "tid1" not in fake_self._background_task_ids
    assert "tid1" not in fake_self._background_task_notifications


def test_referenced_note_is_dropped_early(monkeypatch):
    """The other half of the fix: if the model's response DOES reference the
    injected note, don't wait out the full retry budget -- drop it on the
    very next turn's check."""
    monkeypatch.setattr(task_queue, "get_task_result", lambda tid: _fake_result(result="the answer is 42"))
    fake_self = _fake_agent(response_text="the answer is 42, as promised!",
                             background_task_ids={"tid1"})

    LuminaAgent.chat(fake_self, "what did you find?")
    assert "tid1" in fake_self._background_task_ids  # first injection always happens

    # Next turn: last response ("the answer is 42, as promised!") contains
    # the summary snippet ("the answer is 42") -- should be dropped now,
    # well before the retry budget would otherwise exhaust.
    LuminaAgent.chat(fake_self, "thanks, anything else?")
    assert "tid1" not in fake_self._background_task_ids
    assert "tid1" not in fake_self._background_task_notifications


def test_bare_numeric_summary_does_not_collide_with_larger_numbers(monkeypatch):
    """The live BANANA-scenario re-verification's own summary was a bare
    "144" -- a plain substring check would false-positive against "1445",
    "20144", etc. Confirms the \\b word-boundary match doesn't fire on a
    response that merely CONTAINS the digits as part of a different number,
    and DOES fire on a genuine standalone mention -- checked while attempts
    is still well under BACKGROUND_TASK_NOTIFY_RETRIES, so a drop can only
    be explained by the content match, not coincidental budget exhaustion."""
    monkeypatch.setattr(task_queue, "get_task_result", lambda tid: _fake_result(result="144"))
    fake_self = _fake_agent(response_text="Room 1445 is down the hall, turn 2044 steps.",
                             background_task_ids={"tid1"})

    # Turn 1: task becomes terminal, injected for the first time.
    LuminaAgent.chat(fake_self, "what did you calculate?")
    assert "tid1" in fake_self._background_task_ids

    # Turn 2: Step 1 checks turn 1's response against "144" -- appears only
    # embedded inside "1445" and "2044", must NOT be treated as referenced.
    # This turn's own response now genuinely, standalone mentions it.
    fake_self.llm.response_text = "The result was 144, all done."
    LuminaAgent.chat(fake_self, "unrelated follow-up")
    assert "tid1" in fake_self._background_task_ids, "false-positive collision: '144' matched inside '1445'/'2044'"
    assert fake_self._background_task_notifications["tid1"]["attempts"] == 2

    # Turn 3: Step 1 checks turn 2's response ("The result was 144, all
    # done.") -- a genuine standalone mention. attempts is only 2 here,
    # well under the retry budget of 3, so a drop can only be explained by
    # the match itself, not the budget running out.
    LuminaAgent.chat(fake_self, "ok thanks")
    assert "tid1" not in fake_self._background_task_ids, "standalone '144' should have been recognized as referenced"


def test_two_concurrent_tasks_tracked_independently(monkeypatch):
    """_background_task_notifications is keyed by task_id -- confirm two
    tasks completing in the same window are tracked independently:
    referencing one task's summary in a response must not also silently
    drop the other, unreferenced task's tracking."""
    results = {
        "tid_a": _fake_result(result="the sky is blue"),
        "tid_b": _fake_result(result="water boils at 100C"),
    }
    monkeypatch.setattr(task_queue, "get_task_result", lambda tid: results[tid])
    fake_self = _fake_agent(response_text="just chatting, mentions neither",
                             background_task_ids={"tid_a", "tid_b"})

    # Turn 1: both tasks become terminal and get injected for the first time.
    LuminaAgent.chat(fake_self, "hi")
    assert "tid_a" in fake_self._background_task_ids
    assert "tid_b" in fake_self._background_task_ids
    assert fake_self._background_task_notifications["tid_a"]["attempts"] == 1
    assert fake_self._background_task_notifications["tid_b"]["attempts"] == 1

    # Turn 2: response references ONLY task A's summary. Both notes are
    # re-injected again this turn (attempts -> 2 each) -- the "referenced"
    # check for THIS turn's response only happens at the top of the NEXT
    # chat() call, same as test_referenced_note_is_dropped_early.
    fake_self.llm.response_text = "the sky is blue, as I found out earlier!"
    LuminaAgent.chat(fake_self, "what did you learn?")
    assert "tid_a" in fake_self._background_task_ids
    assert "tid_b" in fake_self._background_task_ids

    # Turn 3: Step 1 now evaluates turn 2's response against both notes.
    fake_self.llm.response_text = "anything else, unrelated"
    LuminaAgent.chat(fake_self, "ok thanks")

    assert "tid_a" not in fake_self._background_task_ids, "referenced task A should be dropped"
    assert "tid_a" not in fake_self._background_task_notifications
    assert "tid_b" in fake_self._background_task_ids, "unreferenced task B must survive independently"
    assert fake_self._background_task_notifications["tid_b"]["attempts"] == 3


def test_task_queue_result_persistence_untouched_by_retry_discard(monkeypatch):
    """The retry-count/discard bookkeeping lives entirely in the agent's own
    in-memory state. Exhausting it must not purge task_queue's own result --
    that's a separate lifecycle (RESULT_TTL_SECONDS), and Part C's Scheduled
    Tasks tab depends on it still being checkable after this agent gives up
    on automatic surfacing."""
    real_tid = task_queue.submit_task(lambda: "a real result")
    for _ in range(50):
        r = task_queue.get_task_result(real_tid)
        if r["status"] != "running":
            break
        time.sleep(0.05)
    assert r["status"] == "success"

    fake_self = _fake_agent(response_text="unrelated, never mentions it",
                             background_task_ids={real_tid})

    for _ in range(BACKGROUND_TASK_NOTIFY_RETRIES + 1):
        LuminaAgent.chat(fake_self, "unrelated question")

    # Agent gave up tracking it...
    assert real_tid not in fake_self._background_task_ids
    # ...but task_queue's own record is completely untouched.
    r_after = task_queue.get_task_result(real_tid)
    assert r_after is not None
    assert r_after["status"] == "success"
    assert r_after["result"] == "a real result"


def test_cancelled_task_is_treated_as_terminal(monkeypatch):
    """S51 Part C added cancel_task() (core/task_queue.py), which a user can
    trigger from the Scheduled Tasks Settings tab without this agent
    otherwise hearing about it. "cancelled" must be treated as terminal the
    same as success/error -- not left to sit in _background_task_ids
    indefinitely (until task_queue's own RESULT_TTL_SECONDS eventually
    expires it, unsurfaced)."""
    monkeypatch.setattr(task_queue, "get_task_result", lambda tid: _fake_result(status="cancelled", result=None))
    fake_self = _fake_agent(response_text="some unrelated reply",
                             background_task_ids={"tid1"})

    LuminaAgent.chat(fake_self, "hello")

    assert "tid1" in fake_self._background_task_ids  # first injection, same as success/error
    assert "cancelled" in fake_self._background_task_notifications["tid1"]["summary"].lower()
