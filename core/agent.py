"""
Lumina Agent Loop
Full turn cycle: receive → think → tool calls → stream final response.
"""

import inspect
import re
import sys
import os
from typing import Optional
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import core.coding_checkpoint as checkpoint_store
from core import emergency_stop
from core.backends.base import TerminationStatus, ToolChoiceMode
from core.backends.loader import get_llm_backend
from core.context import ContextManager
from core.project_context import ProjectContext, ProjectContextState
from tools.registry import ToolRegistry
from tools.meta import register_meta_tools
from tools.memory import register_memory_tools, init_memory_db, init_chat_db
from tools.knowledge import register_knowledge_tools
from tools.web import register_web_tools
from tools.filesystem import register_filesystem_tools
from tools.file_edit import register_file_edit_tools
from tools.search import register_search_tools
from tools.sandbox import register_sandbox_tools
from tools.vision import register_vision_tools
from tools.terminal import register_terminal_tools
from tools.processes import register_process_tools
from tools.coding_checkpoint import register_coding_checkpoint_tools
from tools.tests import register_tests_tools
from tools.worktrees import register_worktree_tools
from tools.review import register_review_tools
from tools.toolmaker import register_toolmaker_tools, load_approved_custom_tools
from tools.palace import register_palace_tools
from core.skills import register_skills_tools, build_skills_block, init_skills_db
from core.chat_history import register_chat_history_tools
from tools.projects import register_projects_tools, init_projects
from tools.diff import register_diff_tools
from tools.browser import register_browser_tools, browser_manager
from tools.telegram_send import register_telegram_tools
from tools.updates import register_update_tools
from tools.git_status import register_git_status_tool
from tools.git_diff import register_git_diff_tool
from tools.git_log import register_git_log_tool
from tools.git_branches import register_git_branches_tool


CHAIN_BLOCKED_AFTER_SEARCH = {"get_website", "web_search"}

# AGENT-CONTINUATION-CONTROL-GATE-01A — two internal continuation-control
# primitives, never product tools: neither is ever registered in
# ToolRegistry, appears in tool inventories/Tool Profiles, or reaches
# registry.call(). Constructed locally here, never offered alongside any
# enabled product tool (see _run_tool_work_control_gate() below) — the
# work-selection request and the completion-control request are two
# structurally separate provider calls with two disjoint tool lists.
#
# This replaces AGENT-REQUIRED-FULL-SCHEMA-01A's live-verified finding:
# offering finish_tool_work alongside the full ~88-tool owner profile under
# REQUIRED made completion an ~89-way forced-choice contest that
# z-ai/glm-5.3-flash intermittently lost to an irrelevant ordinary tool
# (distractor loops, wasted redundant calls, occasionally MAX_TOOL_
# ITERATIONS). A disposable live matrix ruled out repositioning/rewording
# the sentinel within that same forced-choice contest as a fix (moving it
# to the front of the list made things WORSE, not better) — the only
# reliable live fix was removing it from that contest entirely. This is
# that: full product profile + AUTO for real work, then a tiny two-choice
# REQUIRED gate asking only "continue or finish" once a work-selection
# round returns no real tool call. See _run_tool_work_control_gate()'s
# docstring for the full state machine.
#
# Flows through every backend's existing extract_message()/is_tool_call()/
# get_tool_calls()/parse_tool_call() unmodified, the same OpenAI-shaped
# {"type":"function","function":{...}} schema every registered tool
# already uses — zero backend-specific code needed across the OpenAI-
# compatible family, Anthropic, and Gemini.
FINISH_TOOL_WORK_NAME = "finish_tool_work"
CONTINUE_TOOL_WORK_NAME = "continue_tool_work"

_FINISH_TOOL_WORK_SCHEMA = {
    "type": "function",
    "function": {
        "name": FINISH_TOOL_WORK_NAME,
        "description": (
            "Call this when tool work for this turn is complete and you are "
            "ready to give your final answer, instead of just stopping. Do "
            "not call any other tool in the same response as this one."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

_CONTINUE_TOOL_WORK_SCHEMA = {
    "type": "function",
    "function": {
        "name": CONTINUE_TOOL_WORK_NAME,
        "description": (
            "Choose this only if additional real tool work is required "
            "before answering. After choosing it, the full enabled tool "
            "set becomes available again."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

# Sent ONLY by _run_tool_work_control_gate() below — never merged with, or
# offered alongside, the registry's own product-tool schemas. Order is not
# load-bearing here (unlike the old design this replaces): both are tiny,
# and there is no large candidate set for position to matter within.
_CONTROL_GATE_SCHEMAS = [_CONTINUE_TOOL_WORK_SCHEMA, _FINISH_TOOL_WORK_SCHEMA]


def _find_control_call(llm, tool_calls: list, name: str):
    """Return the tool_call dict named `name` if present in `tool_calls`,
    else None. Goes through the backend's own parse_tool_call() rather
    than name-matching a raw provider-specific shape, so this works
    identically across every backend family. Only ever called with
    FINISH_TOOL_WORK_NAME/CONTINUE_TOOL_WORK_NAME by
    _run_tool_work_control_gate() below — the two internal control
    primitives never appear in an ordinary work-selection response."""
    for tc in tool_calls:
        try:
            n, _ = llm.parse_tool_call(tc)
        except Exception:
            continue
        if n == name:
            return tc
    return None


def _extract_commentary(message: dict) -> str:
    """AGENT-COMMENTARY-01A -- return the outward, operator-facing text
    embedded in a tool-bearing (or finish_tool_work-bearing) assistant
    response, or "" if none. Applies the same <think> stripping already
    used for the persisted tool-call message content (strip_think_blocks),
    so hidden reasoning markup never reaches on_commentary(). Pure
    extraction -- never mutates `message`; callers decide whether/when to
    emit and whether the message itself gets persisted to ctx."""
    content = message.get("content")
    if not content:
        return ""
    return strip_think_blocks(content).strip()


def _accepts_tool_choice_mode(llm) -> bool:
    """AGENT-CONTINUATION-01B — True if llm.chat() will accept a
    tool_choice_mode= keyword without raising TypeError: either it
    declares the parameter explicitly (every real backend, post-01B), or
    it accepts **kwargs. Section 7's explicit requirement: "ordinary
    callers need no migration unless necessary" — dozens of hand-rolled
    fake LLM doubles across the test suite predate this parameter with
    fixed positional/keyword chat() signatures; calling them with an
    unexpected kwarg would crash every one of them for no behavioral
    benefit (a fake has no real transport to request anything of).
    Introspection, not a network call — nothing here talks to a provider."""
    try:
        params = inspect.signature(llm.chat).parameters
    except (TypeError, ValueError):
        return False
    if "tool_choice_mode" in params:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


# S51 Part D — how many turns a completed background-task note gets
# re-injected via push_ephemeral() before chat() gives up trying to surface
# it automatically. task_queue's own RESULT_TTL_SECONDS (core/task_queue.py)
# keeps the actual result available well past this regardless — this only
# bounds the automatic notify-later attempts, not how long the result stays
# checkable via check_background_task / the Scheduled Tasks tab.
BACKGROUND_TASK_NOTIFY_RETRIES = 3


class TurnCancelled(Exception):
    """Cooperative foreground-turn cancellation at a safe agent boundary."""
    def __init__(self, partial_response: str = ""):
        super().__init__("foreground turn cancelled by operator")
        self.partial_response = partial_response or ""


class TurnCancellation:
    """Per-agent holder for the current turn's cooperative /stop cancel_event
    (CODING-06A2). Mirrors core/project_context.py's ProjectContextState --
    per-instance, never a process global. A tool registrar closure bound at
    agent-construction time (before any cancel_event exists) reads .get() at
    call time, so a blocking tool call always observes whichever cancel_event
    is active for the turn actually running it, never a stale one from a
    previous turn or a different agent instance."""

    def __init__(self):
        self._event = None

    def get(self):
        return self._event

    def _set(self, event):
        self._event = event


def _cancel_requested(cancel_event) -> bool:
    """True if the normal per-worker /stop Event is set, OR the current
    emergency execution is no longer permitted (latched, or this
    execution's epoch has gone stale). This makes every existing /stop
    safe boundary an E-stop boundary automatically, with no second set of
    checks sprinkled through the tool loop."""
    if cancel_event is not None and cancel_event.is_set():
        return True
    return not emergency_stop.execution_permitted()


def _close_cancelled_tool_calls(agent, tool_calls):
    """Resolve unexecuted tool ids without laundering them as TOOL_OUTPUT."""
    for tc in tool_calls:
        tool_id = tc.get("id", "unknown") if isinstance(tc, dict) else "unknown"
        try:
            name, _ = agent.llm.parse_tool_call(tc)
        except Exception:
            name = "cancelled_tool"
        agent.ctx.add_cancelled_tool_result(tool_id, name)


def _provider_chat_or_error(agent, chat_kwargs: dict, cancel_event, tools_used_this_turn: set):
    """Call agent.llm.chat(**chat_kwargs); on success return (response,
    None). On a genuine provider exception, print the same diagnostic the
    tool loop has always printed and return (None, error_text) -- ready to
    return directly from chat(), never raised further (chat() never raises
    for a provider failure -- see its own docstring). Cancellation observed
    while the exception is being handled still raises TurnCancelled,
    exactly as before this helper existed.

    Module-level and duck-typed against `agent` (never a bound method),
    same convention as _close_cancelled_tool_calls() above -- so the
    existing types.SimpleNamespace fake-agent pattern across this file's
    tests keeps working with zero extra method-binding, only real llm/ctx/
    on_response_token attributes.

    Shared by both the WORK-phase request and _run_tool_work_control_gate()
    below, so a provider rejecting either one gets identical "tool already
    ran, provider rejected the continuation" observability -- the gate's
    own request IS a tool-continuation request, same as the old design's
    post-tool round was, so tools_used_this_turn is always already
    non-empty by the time this is called for a gate request."""
    try:
        return agent.llm.chat(**chat_kwargs), None
    except Exception as e:
        if _cancel_requested(cancel_event):
            raise TurnCancelled()
        provider = getattr(agent.llm, "display_name", None) or getattr(agent.llm, "name", "the provider")
        get_model = getattr(agent.llm, "get_model", None)
        model = get_model() if callable(get_model) else "unknown"
        stage = "tool_continuation" if tools_used_this_turn else "initial_request"
        print(f"[AGENT ERROR] provider={provider} model={model} "
              f"stage={stage} {type(e).__name__}: {e}", flush=True)
        if tools_used_this_turn:
            # A tool already ran successfully this turn — the failure is
            # the PROVIDER rejecting the continuation request, not a
            # silent hang. Name it explicitly so this doesn't read as
            # "the model just stopped responding" — the actual symptom
            # Bug B produced before the thoughtSignature-preservation
            # fix in gemini_backend.py. Streamed via on_response_token
            # (the same path _stream_final's own error handler already
            # uses) rather than just returned, because agent.chat()
            # deliberately never raises — main.py's CLI loop calls it
            # with no try/except around the call, relying on that
            # contract — so the only way to make this visible in the
            # GUI without touching that contract is to push it through
            # the token-streaming callback the GUI already renders live
            # (AgentWorker → _on_response_chunk → the live bubble),
            # rather than routing through the separate signals.error /
            # _on_error path, which only fires on a raised exception and
            # is never reached from inside this try/except at all.
            just_ran = ", ".join(f"`{n}`" for n in sorted(tools_used_this_turn))
            err = f"[Tool {just_ran} completed, but {provider} rejected the continuation: {e}]"
        else:
            err = f"[Lumina error: {e}]"
        on_response_token = getattr(agent, "on_response_token", None)
        if callable(on_response_token):
            on_response_token(err)
        return None, err


def _run_tool_work_control_gate(agent, tools_used_this_turn: set, cancel_event,
                                 reasoning_effort, chat_id, think_step: list):
    """AGENT-CONTINUATION-CONTROL-GATE-01A -- ask ONLY the two internal
    continuation-control primitives (_CONTROL_GATE_SCHEMAS — never a
    product tool) whether real tool work for this turn is complete. Only
    ever called by _chat_impl()'s tool loop after at least one real tool
    has already executed this turn and a WORK round returned no real tool
    call — never on a genuine first-round answer.

    Module-level and duck-typed against `agent`, same convention as
    _provider_chat_or_error() above -- no method binding needed on a fake.

    Returns (outcome, error_text):
      ("finish", None)      -- stream the final answer
      ("continue", None)    -- return to a full-profile WORK round
      ("malformed", None)   -- both or neither control selected
      ("incomplete", None)  -- response was positively truncated
      ("cancelled", None)   -- caller must raise TurnCancelled
      ("error", text)       -- provider exception; text is ready to
                                return directly from chat()

    think_step (AGENT-TOOL-THINK-TELEMETRY-01A1): the SAME shared, mutable
    [int] counter list _chat_impl() seeds and _stream_final() also
    advances -- passed through (never a fresh local counter here) so a
    Think event fired during this gate's own request numbers into the
    same monotonic per-turn sequence as WORK-round and final-stream Think
    events. See _emit_tool_round_think()'s docstring.

    Uses the existing one-turn ephemeral system-prompt injection
    (ctx.push_ephemeral/build_messages) for the gate's own instruction --
    consumed by this call's own build_messages() and gone by the time any
    later request is built, exactly like every other ephemeral nudge in
    this file, so it never leaks into a later WORK round or the final
    stream. Never persists a synthetic user or assistant message, never
    dispatches through registry.call(), never touches _session_tool_calls
    or the skill-nudge threshold, never fires on_tool_call/on_tool_result
    -- the isolation contract AGENT-CONTINUATION-01A established for
    finish_tool_work applies identically to both primitives here."""
    agent.ctx.push_ephemeral(
        "## Tool-work completion gate\n"
        "Decide whether additional real tool work is required. "
        "Call continue_tool_work if more tool work is needed. "
        "Call finish_tool_work if tool work is complete. "
        "Choose exactly one."
    )
    gate_budget = sum(len(str(s)) // 4 for s in _CONTROL_GATE_SCHEMAS)
    messages = agent.ctx.build_messages(tool_budget=gate_budget, chat_id=chat_id)
    if _cancel_requested(cancel_event):
        return "cancelled", None

    chat_kwargs = dict(
        messages=messages,
        tools=_CONTROL_GATE_SCHEMAS,
        max_tokens=config.RESPONSE_RESERVE_TOKENS,
        reasoning_effort=reasoning_effort,
    )
    if _accepts_tool_choice_mode(agent.llm):
        # Always REQUEST required here -- resolution against this
        # backend's live-verified capability is the backend layer's job
        # (BaseLLMBackend._resolve_tool_choice_mode / each concrete
        # chat()), exactly the separation of concerns AGENT-CONTINUATION-
        # 01B established: the agent expresses intent, only a backend with
        # supports_required_tool_choice = True does anything different
        # with it; every other backend silently keeps this on AUTO. This
        # is now the ONLY place in the whole tool loop that ever requests
        # REQUIRED.
        chat_kwargs["tool_choice_mode"] = ToolChoiceMode.REQUIRED

    response, err = _provider_chat_or_error(agent, chat_kwargs, cancel_event, tools_used_this_turn)
    if err is not None:
        return "error", err
    if _cancel_requested(cancel_event):
        return "cancelled", None

    message = agent.llm.extract_message(response)
    _extract_termination = getattr(agent.llm, "extract_termination", None)
    termination = (_extract_termination(response) if callable(_extract_termination)
                   else TerminationStatus.UNKNOWN)
    if termination == TerminationStatus.INCOMPLETE:
        return "incomplete", None

    has_tool_calls = agent.llm.is_tool_call(message)
    tool_calls = agent.llm.get_tool_calls(message) if has_tool_calls else []
    finish_call = _find_control_call(agent.llm, tool_calls, FINISH_TOOL_WORK_NAME)
    continue_call = _find_control_call(agent.llm, tool_calls, CONTINUE_TOOL_WORK_NAME)

    if finish_call is not None and continue_call is not None:
        outcome = "malformed"
    elif finish_call is not None:
        outcome = "finish"
    elif continue_call is not None:
        outcome = "continue"
    else:
        outcome = "malformed"

    if outcome in ("finish", "continue"):
        # AGENT-TOOL-THINK-TELEMETRY-01A1 -- Think, then a cancellation
        # check, then Commentary: same required ordering/boundary as the
        # WORK round below (_chat_impl()'s own tool-bearing branch),
        # applied here without touching this gate's existing
        # non-persistence contract -- nothing below reads or writes
        # agent.ctx. Gated on the same (finish, continue) outcomes
        # commentary itself is already scoped to, not every outcome --
        # a malformed/incomplete gate response gets retried, never
        # surfaced as if it were a real tool-decision round.
        reasoning = _collect_tool_round_reasoning(agent.llm, response, message)
        _emit_tool_round_think(agent, think_step, reasoning)
        if _cancel_requested(cancel_event):
            return "cancelled", None

        commentary = _extract_commentary(message)
        if commentary:
            on_commentary = getattr(agent, "on_commentary", None)
            if callable(on_commentary):
                on_commentary(commentary)

    return outcome, None


def strip_think_blocks(text: str) -> str:
    if not text:
        return text
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()


# AGENT-TOOL-THINK-TELEMETRY-01A1 -- same span definition strip_think_blocks()
# uses above (non-greedy, DOTALL), with a capture group added so the exact
# text about to be destroyed can be recovered first. Kept as a genuinely
# separate regex object (not a refactor of strip_think_blocks() to reuse
# one pattern) so a future edit to either can't silently desync the other
# without a visible diff in both places.
_INLINE_THINK_RE = re.compile(r'<think>(.*?)</think>', re.DOTALL)


def _extract_inline_think(text) -> Optional[str]:
    """AGENT-TOOL-THINK-TELEMETRY-01A1 -- return the concatenated text of
    every <think>...</think> span in `text`, or None if there are none (or
    `text` isn't a non-empty string). Must be called BEFORE
    strip_think_blocks() runs on the same text -- that function's whole
    job is to erase exactly what this one recovers; call order is what
    keeps this observability addition from starting to fight the existing
    anti-bleed contract instead of sitting in front of it."""
    if not isinstance(text, str) or not text:
        return None
    matches = _INLINE_THINK_RE.findall(text)
    if not matches:
        return None
    joined = "\n\n".join(m.strip() for m in matches if m.strip())
    return joined or None


def _collect_tool_round_reasoning(llm, response, message: dict) -> Optional[str]:
    """AGENT-TOOL-THINK-TELEMETRY-01A1 -- one-shot, best-effort collection
    of whatever legitimate provider reasoning already exists for a single
    tool-decision round: a backend-declared field (llm.extract_reasoning())
    and/or inline <think> tags embedded in the raw (not-yet-stripped)
    message content. Neither source is fabricated or requested -- this
    only reads what already arrived on `response`/`message`. Never raises:
    a backend without extract_reasoning() at all, or one whose
    extract_reasoning() itself raises, degrades to "no field-based
    reasoning" rather than breaking the tool loop over an observability
    add-on. Returns None (never an empty string) when nothing was found in
    either source, so callers can treat "None" as the single, unambiguous
    "no Think event this round" signal."""
    parts = []

    extract_reasoning = getattr(llm, "extract_reasoning", None)
    if callable(extract_reasoning):
        try:
            field_reasoning = extract_reasoning(response)
        except Exception:
            field_reasoning = None
        if isinstance(field_reasoning, str):
            stripped = field_reasoning.strip()
            if stripped:
                parts.append(stripped)

    inline = _extract_inline_think(message.get("content"))
    if inline:
        parts.append(inline)

    if not parts:
        return None
    return "\n\n".join(parts)


def _emit_tool_round_think(agent, think_step: list, reasoning: Optional[str]) -> None:
    """AGENT-TOOL-THINK-TELEMETRY-01A1 -- fire one bulk Think event
    (start/one token/end) for a tool-decision round's already-collected
    reasoning, using the SAME shared `think_step` counter _stream_final()
    advances -- so WORK/GATE/final Think numbering is one monotonic
    per-turn sequence, never separate per-phase counters (see this
    method's own call sites for why think_step is threaded through as a
    shared mutable [int] rather than a local variable per phase).

    No-ops entirely (no start, no end, no counter advance) when
    `reasoning` is None/empty -- there must never be an empty Think
    widget for a round that had nothing to show. Requires all three
    callbacks to be callable before firing any of them; a fake/legacy
    agent missing one degrades to silently skipping Think for this round
    rather than partially firing and crashing on the missing one -- the
    same getattr-guarded fail-inert posture as every other optional-
    capability check in this file (see _accepts_tool_choice_mode()'s own
    docstring for the precedent)."""
    if not reasoning:
        return
    on_think_start = getattr(agent, "on_think_start", None)
    on_think_token = getattr(agent, "on_think_token", None)
    on_think_end = getattr(agent, "on_think_end", None)
    if not (callable(on_think_start) and callable(on_think_token) and callable(on_think_end)):
        return
    think_step[0] += 1
    on_think_start(think_step[0])
    on_think_token(reasoning)
    on_think_end()


# Sentinel prefixes chat()'s tool-loop except-block and _stream_final()'s
# except-block use to report a failed turn as plain response text instead
# of raising (see docstrings on both). Centralized here rather than
# duplicated as string-matching in the UI layer, which used to have no way
# to tell a failed turn from a real assistant reply — see Bug C fix,
# LUMINA_HANDOFF_OMNIROUTE_TO_PROVIDER_FIXES_2026-08-02.md section 8.3:
# "Suppress auto-name when the main turn fails."
ERROR_RESPONSE_PREFIXES = ("[Lumina error:", "[Stream error:")

# A third failure sentinel — a tool call succeeds, then the *next* provider
# call in the same turn fails during continuation (Bug B / section 8.2.1's
# observability fix, added to the tool loop's except-block last session).
# That fix isn't in the public repo this module's tests run against, so its
# exact bracket/prefix formatting can't be verified here — only the
# templated wording itself, quoted verbatim in both
# LUMINA_HANDOFF_OMNIROUTE_TO_PROVIDER_FIXES_2026-08-02.md section 8.2.1
# and the Bug C patch-review report that found this gap. Anchoring on that
# substring instead of guessing the surrounding prefix: if the actual
# wording differs, update this rather than trust it silently.
ERROR_RESPONSE_SUBSTRINGS = ("rejected the continuation",)


def is_error_response(text: str) -> bool:
    """True if `text` is one of chat()/_stream_final()'s sentinel failure
    strings rather than a genuine assistant reply. Callers (e.g. the UI's
    auto-name trigger) should check this before treating `response` as
    real conversational content."""
    if not text:
        return False
    if text.startswith(ERROR_RESPONSE_PREFIXES):
        return True
    return any(s in text for s in ERROR_RESPONSE_SUBSTRINGS)


class LuminaAgent:
    def __init__(self,
                 on_tool_call=None,
                 on_tool_result=None,
                 on_think_start=None,
                 on_think_token=None,
                 on_think_end=None,
                 on_response_token=None,
                 on_commentary=None,
                 tts=None,
                 owner: bool = True,
                 channel_id: str = "default",
                 depth: int = 0,
                 backend: str = None,
                 project_context: Optional[ProjectContext] = None,
                 _review_target_grant: Optional[
                     checkpoint_store.TargetIdentity
                 ] = None):
        """
        Streaming callbacks:
          on_tool_call(name, args)     — tool about to execute
          on_tool_result(name, result) — tool finished
          on_think_start(step)         — <think> block opened
          on_think_token(token)        — character inside think block
          on_think_end()               — </think> block closed
          on_response_token(token)     — final response token streaming
          on_commentary(text)          — AGENT-COMMENTARY-01A: brief
              operator-facing narration the model gave alongside a
              structured tool decision (a real tool call, or
              finish_tool_work). Fires once per provider response that
              carries such narration, BEFORE the tool call(s) it
              describes execute. Distinct from on_response_token (final
              answer only) and from the Think callbacks above (provider
              reasoning telemetry, not present at all on this
              non-streaming tool-decision path — see _chat_impl()).
              Display-only: never fed back into the model, never
              persisted as a synthetic message. See _extract_commentary().

        owner: True for the desktop app (you). False for ANY agent constructed
        on behalf of a channel, subagent, or scheduled task — no implicit
        default, every call site decides this explicitly.
        channel_id: groups PIN verification/lockout state per channel.
        depth: subagent nesting level, 0 for any top-level agent (desktop, Telegram,
            Discord, background tasks). Only ever incremented by spawn_subagent()
            itself when constructing a child — never accept this from a tool-call
            argument, the model must never set its own depth.
        backend: optional backend name override, passed straight to get_llm_backend().
            None (default) preserves current behavior — config.LLM_BACKEND.
        project_context (CODING-02B-A): optional ProjectContext this agent
            starts with. None (default, every existing caller) means no
            active project — every project-aware tool falls back to its
            pre-CODING-02B-A legacy path/cwd behavior exactly. Deliberately
            NOT resolved from a project *name* here — the constructor only
            ever accepts an already-resolved, immutable ProjectContext, so
            CODING-02B-B's subagent/background-task dispatch can capture one
            once at dispatch time and hand it straight through, rather than
            this constructor re-resolving a name (and potentially a since-
            changed binding) itself.
        _review_target_grant (CODING-08A3): internal only, never a tool-call
            argument. This agent's exact, immutable Git review-target
            authority for tools/review.py's review_changes/review_file_diff,
            or None (no review authority). The ONLY caller that ever sets
            this to a non-None value is tools/subagent.py's spawn_subagent(),
            for a child dispatched into an exact managed worktree -- see its
            docstring. Deliberately a separate field from project_context:
            a synthetic worktree ProjectContext is ergonomic path defaulting,
            never authority (see tools/review.py's module docstring), and
            this value is never re-read from a mutable ProjectContextState
            that a later-granted activate_project() call could repoint.
        """
        self.llm = get_llm_backend(name=backend)
        self.owner = owner
        self._subagent_depth = depth
        self.ctx = ContextManager(owner=owner)
        self.registry = ToolRegistry()
        self.channel_id = channel_id
        # Per-instance holder, never a module/process global — see
        # core/project_context.py's own module docstring for why. Two
        # LuminaAgent instances always get two distinct holders.
        self.project_context = ProjectContextState(project_context)
        # CODING-08A3: immutable for this agent's whole lifetime -- assigned
        # exactly once, here, never reassigned by any tool or later code path.
        self._review_target_grant = _review_target_grant
        # CODING-06A2: per-instance holder for the current turn's cooperative
        # /stop cancel_event, same per-instance-not-global convention as
        # project_context above. Set at the top of a turn, cleared when it
        # ends -- see chat() below.
        self.turn_cancellation = TurnCancellation()

        if owner:
            # FE-09: one-time, idempotent — moves any cloud API keys still
            # sitting in prefs.json (from before secrets.py handled them)
            # into proper credential storage. No-op after the first run.
            from core.secrets import migrate_legacy_cloud_keys
            migrate_legacy_cloud_keys()

        self.on_tool_call     = on_tool_call     or (lambda n, a: None)
        self.on_tool_result   = on_tool_result   or (lambda n, r: None)
        self.on_think_start   = on_think_start   or (lambda step: None)
        self.on_think_token   = on_think_token   or (lambda t: None)
        self.on_think_end     = on_think_end     or (lambda: None)
        self.on_response_token = on_response_token or (lambda t: None)
        self.on_commentary    = on_commentary    or (lambda t: None)
        self.tts = tts
        # Persona-local speech policy. This stays independent of the
        # backend/global TTS enabled state so another persona can reuse the
        # already-loaded backend immediately.
        self._persona_speech_suppressed = False
        self.persona_avatar = None  # set by apply_persona()
        self.current_persona = None  # set by apply_persona() -- lets Settings recombine the global prompt + persona identity when the global prompt is live-edited
        self._session_tool_calls = 0       # total tool calls this session
        self._skill_nudge_sent   = False   # only nudge once per session
        self._background_task_ids: set = set()  # dispatched background/scheduled task_ids awaiting an ephemeral completion notice; only meaningful for owner=True desktop sessions, harmless elsewhere
        self._background_task_notifications: dict = {}  # task_id -> {"summary", "attempts"} for terminal tasks mid-retry (S51 Part D) -- separate lifecycle from task_queue's own result TTL

        init_memory_db()
        init_chat_db()
        register_meta_tools(self.registry, self.ctx)
        register_memory_tools(self.registry)
        register_knowledge_tools(self.registry)
        register_web_tools(self.registry)
        register_filesystem_tools(self.registry, project_state=self.project_context)
        register_file_edit_tools(self.registry, project_state=self.project_context)
        register_search_tools(self.registry, project_state=self.project_context)
        register_sandbox_tools(self.registry)
        register_vision_tools(self.registry)
        register_terminal_tools(self.registry, project_state=self.project_context)
        register_process_tools(
            self.registry,
            project_state=self.project_context,
            channel_id=self.channel_id,
        )
        register_coding_checkpoint_tools(
            self.registry,
            project_state=self.project_context,
        )
        register_tests_tools(
            self.registry,
            project_state=self.project_context,
            cancel_state=self.turn_cancellation,
        )
        self._resolve_worktree_dispatch = register_worktree_tools(
            self.registry,
            project_state=self.project_context,
            cancel_state=self.turn_cancellation,
        )
        register_review_tools(
            self.registry,
            owner=owner,
            project_state=self.project_context,
            worktree_resolver=self._resolve_worktree_dispatch,
            review_target_grant=self._review_target_grant,
        )
        if owner:
            # Hard exclusion — for non-owner sessions, toolmaker's tools never
            # exist in the registry at all. Not disabled, not absent from a
            # profile — absent from _tools, period.
            register_toolmaker_tools(self.registry, self)
        register_palace_tools(self.registry)
        from tools.pin import register_pin_tools
        register_pin_tools(self.registry, channel_id)

        init_skills_db()
        register_skills_tools(self.registry)   
        register_chat_history_tools(self.registry) 
        init_projects()
        register_projects_tools(self.registry, project_state=self.project_context)
        register_diff_tools(self.registry)
        register_browser_tools(self.registry)
        register_telegram_tools(self.registry)
        register_update_tools(self.registry)
        # Promoted from the toolmaker custom-tool pipeline (was approved via
        # create_tool -> approve_pending_tool, then hand-promoted into tracked
        # source once reviewed and hardened) — same as get_weather's history,
        # but these are wired statically here rather than left to the FE-11
        # loader below, since they're now genuine built-ins, not custom tools.
        register_git_status_tool(self.registry, project_state=self.project_context)
        register_git_diff_tool(self.registry, project_state=self.project_context)
        register_git_log_tool(self.registry, project_state=self.project_context)
        register_git_branches_tool(self.registry, project_state=self.project_context)

        # FE-11: reload any custom tool that was approved through the
        # toolmaker review pipeline in a past session. Not owner-gated —
        # a tool like get_weather is an ordinary tool once approved, not a
        # toolmaker-management tool, so it follows the same visibility path
        # as everything else below (default-deny for non-owner, restored
        # only by an explicit profile).
        _loaded_custom = load_approved_custom_tools(self.registry)
        if _loaded_custom:
            print(f"[AGENT] Loaded approved custom tools: {', '.join(_loaded_custom)}", flush=True)

        # Subagents + background/scheduled tasks — own flags, independent of
        # each other (see config.py). Registered unconditionally by owner —
        # a non-owner subagent can itself spawn a further subagent (subject
        # to MAX_SUBAGENT_DEPTH), it just won't have spawn_subagent actually
        # enabled unless the parent explicitly granted it via tools_enabled;
        # the default-deny sweep below still applies to non-owner sessions
        # same as every other tool registered above.
        if config.SUBAGENTS_ENABLED:
            from tools.subagent import register_subagent_tools
            register_subagent_tools(
                self.registry, self._subagent_depth,
                project_state=self.project_context,
                worktree_resolver=self._resolve_worktree_dispatch,
            )

        if config.BACKGROUND_TASKS_ENABLED:
            from tools.tasks import register_task_tools
            register_task_tools(
                self.registry, self,
                worktree_resolver=self._resolve_worktree_dispatch,
            )

        # Default-deny resolution runs LAST — after every register_*_tools()
        # call above. Anything registered before this line and not restored
        # by an explicit profile stays locked for non-owner sessions. Moving
        # this earlier reopens the gap the S34 smoke test caught: tools
        # registered after the snapshot were never added to _disabled and
        # came up enabled by default.
        if owner:
            from core.persistence import load as load_prefs
            _disabled_tools = load_prefs().get("disabled_tools", [])
            if _disabled_tools:
                self.registry.set_disabled(_disabled_tools)
        else:
            # Non-owner: default-deny everything until a persona/profile
            # explicitly opts tools back in. No window of inherited owner state.
            self.registry.set_disabled(self.registry.all_tool_names())

        if not owner:
            from core.pin_gate import is_verified
            from core.tool_profiles import TOOL_TIERS
            SENSITIVE_TIERS = {"execute", "self_modifying", "outbound_action"}
            def _gate(name):
                tier = TOOL_TIERS.get(name, "execute")  # unclassified tool = fail closed
                if tier in SENSITIVE_TIERS and not is_verified(channel_id):
                    return False, "PIN verification required for this action."
                return True, ""
            self.registry.set_gate(_gate)

    def chat(self, user_input: str, source: str = "OWNER_DIRECT", chat_id: int = None,
             cancel_event=None, reasoning_effort: Optional[str] = None) -> str:
        """
        Main entry point. Runs tool loop with non-streaming,
        then streams the final response. Returns full response string.
        source: passed straight through to ctx.add_user(). OWNER_DIRECT (default)
        preserves current desktop behavior unchanged.
        chat_id (MB-11): threaded straight through to ctx.build_messages() for
        the session-pin read-side fix — see core/context.py's
        _build_system_prompt() docstring. None (default) preserves current
        behavior for every caller that doesn't track a chat_id (CLI, headless,
        subagents).
        cancel_event: optional threading.Event owned by the desktop foreground
        worker. Cancellation is cooperative: already-blocked provider/tool calls
        are allowed to return, then chat() exits at the next safe boundary.
        reasoning_effort (Patch 3A.4 Part 3): the caller's raw requested
        reasoning-effort selection for this ENTIRE turn — the same value is
        used for the initial request, every tool-continuation, and the
        final stream (see _chat_impl()/_stream_final()). Per-call state
        only: never stored on self, a backend instance, config, or prefs.
        None (default) preserves current behavior for every existing
        caller — main.py, ui/main_window.py, core/headless.py,
        tools/subagent.py — none of which pass this yet. core/agent.py
        never inspects/validates this value itself; it's forwarded
        unchanged all the way down to whichever backend is active, which
        owns validation/translation entirely via its own
        reasoning_capabilities()/apply_reasoning().

        Thin wrapper — the real body lives in _chat_impl(), unbound-called
        via LuminaAgent._chat_impl(self, ...) rather than self._chat_impl(...)
        so existing tests that call LuminaAgent.chat(fake_self, ...) against a
        minimal types.SimpleNamespace stand-in stay valid. Every real turn
        runs inside an emergency execution_scope for its whole lifetime — see
        core/emergency_stop.py. Metadata is deliberately small and safe
        (channel id, owner bool, chat id); the user prompt itself is never
        snapshotted here.

        3A.2 Part A: a worker can start just before an operator latches
        E-stop, losing the execution_scope() admission race at entry —
        before _chat_impl() (and its own add_user()) ever runs. That must
        surface as an ordinary TurnCancelled, not a generic error, and must
        still preserve the submitted user turn in live context exactly like
        every other pre-work cancellation path (2B2's own already-requested
        check does the same thing at the top of _chat_impl()). Only
        EmergencyStopError is caught here — never a bare Exception — so an
        unrelated programmer error out of execution_scope() still surfaces
        as itself.
        """
        # CODING-06A2: getattr-guarded, same reason as every other fake-self
        # compatibility check in this method — lightweight test stand-ins
        # (types.SimpleNamespace) predating this patch have no
        # turn_cancellation attribute at all. Real LuminaAgent instances
        # always have one, set in __init__.
        turn_cancellation = getattr(self, "turn_cancellation", None)
        if turn_cancellation is not None:
            turn_cancellation._set(cancel_event)
        try:
            try:
                with emergency_stop.execution_scope(
                    kind="foreground_turn",
                    label=getattr(self, "channel_id", "default"),
                    metadata={
                        "channel_id": getattr(self, "channel_id", "default"),
                        "owner": getattr(self, "owner", None),
                        "chat_id": chat_id,
                    },
                ):
                    return LuminaAgent._chat_impl(
                        self, user_input, source=source, chat_id=chat_id,
                        cancel_event=cancel_event, reasoning_effort=reasoning_effort,
                    )
            except emergency_stop.EmergencyStopError:
                self.ctx.add_user(user_input, source=source)
                raise TurnCancelled()
        finally:
            if turn_cancellation is not None:
                turn_cancellation._set(None)

    def _chat_impl(self, user_input: str, source: str = "OWNER_DIRECT", chat_id: int = None,
                    cancel_event=None, reasoning_effort: Optional[str] = None) -> str:
        tools_used_this_turn = set()
        think_step = [0]
        # AGENT-CONTINUATION-01A — per-turn bound on the corrective retry
        # below, not per-occurrence: this whole foreground turn gets at
        # most one automatic "you didn't call a tool or confirm completion"
        # nudge, ever, however many ambiguous moments it hits. A second
        # occurrence after the retry has already been spent goes straight
        # to the visible non-success path instead of ping-ponging retries.
        corrective_retry_used = False

        # Preserve the user's submitted turn even in the tiny race where /stop
        # lands before this worker gets past chat()'s prologue. Do not run any
        # background-notification bookkeeping or provider work in that case.
        if _cancel_requested(cancel_event):
            self.ctx.add_user(user_input, source=source)
            raise TurnCancelled()

        # Background/scheduled task completions surface as a one-turn
        # ephemeral injection, same mechanism as skill docs below. owner=True
        # only — _background_task_ids is only ever populated by this agent's
        # own registered run_background_subagent/schedule_background_subagent
        # tool wrappers (tools/tasks.py), only reachable from a real session.
        # push_ephemeral() OVERWRITES rather than appends (core/context.py) —
        # collected here into task_summaries and combined with skills_block
        # below into a single call, instead of two push_ephemeral() calls
        # that would silently stomp each other.
        #
        # Runs BEFORE ctx.add_user() below, deliberately — the "was last
        # turn's injected note referenced" check reads self.ctx.history[-1],
        # which is only still last turn's assistant response if we look
        # before this turn's user message gets appended.
        #
        # getattr, not direct attribute access -- several existing unit tests
        # (test_agent_tool_budget.py, test_agent_tool_continuation.py) call
        # LuminaAgent.chat(fake_self, ...) unbound against a minimal
        # types.SimpleNamespace stand-in that only sets ctx/registry/llm.
        # Real LuminaAgent instances always have both attributes from
        # __init__; this is purely so those lighter-weight fakes keep working
        # unchanged rather than needing every one of them updated.
        task_summaries = []
        if getattr(self, "owner", False) and getattr(self, "_background_task_ids", None):
            from core.task_queue import get_task_result
            notifications = getattr(self, "_background_task_notifications", None)
            if notifications is None:
                notifications = {}

            # Step 1 — a note injected last turn either got referenced by
            # the model's own response, or it didn't. Bug #2 (S51 Part A):
            # discarding unconditionally on terminal status meant a note
            # shown exactly once, with no retry, silently vanished forever
            # the moment the model's attention went elsewhere that turn —
            # which live testing showed is the common case, not an edge
            # case. Detection is deliberately crude (a short substring
            # check), not sophisticated content-matching — the bounded
            # retry count below is the real backstop, not this check.
            #
            # \b word-boundary wrap, not a bare substring check — the live
            # scenario's own summary was a bare "144", and a plain `in`
            # check would false-positive against "1445", "20144", etc.
            # Short numeric summaries (counts, calculations, IDs) are the
            # common case for task-queue-adjacent work, not a hypothetical
            # edge case, so this is worth the two extra lines even under
            # "don't over-engineer" — it's still not semantic
            # acknowledgment-detection, just a tighter substring match.
            last_assistant_text = ""
            if self.ctx.history and self.ctx.history[-1].get("role") == "assistant":
                last_assistant_text = str(self.ctx.history[-1].get("content") or "").lower()
            for tid in list(notifications):
                entry = notifications[tid]
                snippet = entry["summary"][:40].strip().lower()
                referenced = bool(snippet) and bool(
                    re.search(r'\b' + re.escape(snippet) + r'\b', last_assistant_text)
                )
                if referenced or entry["attempts"] >= BACKGROUND_TASK_NOTIFY_RETRIES:
                    notifications.pop(tid, None)
                    self._background_task_ids.discard(tid)

            # Step 2 — build this turn's note from whatever's still tracked:
            # tasks already mid-retry (reuse the cached summary — task_queue's
            # own RESULT_TTL_SECONDS is a SEPARATE lifecycle from this
            # in-memory retry state, so an expired task_queue entry doesn't
            # cut a retry cycle short) plus any newly-terminal task reaching
            # completion for the first time this turn.
            for tid in list(self._background_task_ids):
                if tid in notifications:
                    summary = notifications[tid]["summary"]
                else:
                    r = get_task_result(tid)
                    if r is None:
                        # Expired out of task_queue before we ever got a
                        # chance to surface it at all -- nothing to retry.
                        self._background_task_ids.discard(tid)
                        continue
                    if r["status"] not in ("success", "error", "cancelled"):
                        continue  # still running/scheduled
                    if r["status"] == "cancelled":
                        # S51 Part C — cancel_task() (core/task_queue.py) lets
                        # the Scheduled Tasks Settings tab cancel a task this
                        # agent doesn't otherwise hear about. Terminal, same
                        # as success/error -- must not sit here silently
                        # until task_queue's own RESULT_TTL_SECONDS expiry
                        # eventually cleans it up unsurfaced.
                        summary = "cancelled before it started"
                    elif r["status"] == "success":
                        # spawn_subagent() never raises, so a "success" task_queue
                        # status wraps spawn_subagent's OWN {"success","result","error"}
                        # dict, not a plain string — unwrap it rather than dumping
                        # the raw dict repr into the prompt.
                        inner = r["result"]
                        if isinstance(inner, dict) and "success" in inner:
                            summary = inner["result"] if inner["success"] else f"failed: {inner['error']}"
                        else:
                            summary = str(inner)
                    else:
                        summary = f"failed: {r['result']}"
                    notifications[tid] = {"summary": summary, "attempts": 0}

                task_summaries.append(f"[Background task {tid} completed: {summary}]")
                notifications[tid]["attempts"] += 1

            self._background_task_notifications = notifications

        self.ctx.add_user(user_input, source=source)

        # Inject relevant skill docs into system prompt for this turn.
        # Skill injection is a nice-to-have — never allowed to kill the turn.
        # (Image turns pass multipart list content in; build_skills_block is
        # type-safe now, but a failure here must not brick the session.)
        try:
            skills_block = build_skills_block(user_input)
        except Exception as e:
            print(f"[SKILLS] build_skills_block skipped: {e}", flush=True)
            skills_block = ""

        ephemeral_parts = task_summaries + ([skills_block] if skills_block else [])
        if ephemeral_parts:
            self.ctx.push_ephemeral("\n\n".join(ephemeral_parts))

        # MB-03 — soft ceiling, warning only. No mechanism yet exists to narrow
        # tool schemas to fit (that's MB-10's job); this just turns "we don't know
        # if schema bloat is a problem" into a real, visible signal.
        _schema_tokens = self.registry.schema_token_estimate()
        if _schema_tokens > config.TOOL_BUDGET_TOKENS:
            print(f"[TOOLS] schema budget exceeded: {_schema_tokens} tokens > "
                  f"{config.TOOL_BUDGET_TOKENS} configured ceiling "
                  f"({len(self.registry.list_enabled())} enabled tools)", flush=True)

        # AGENT-CONTINUATION-CONTROL-GATE-01A -- explicit state for which
        # request this iteration sends: "work" (full enabled product
        # profile, AUTO) or "gate" (the two internal continuation-control
        # primitives only, REQUIRED where supported). A malformed/
        # incomplete GATE response retries the GATE itself (re-asks the
        # same two-choice question) rather than falling through to a WORK
        # round that doesn't even offer continue/finish -- see
        # _run_tool_work_control_gate()'s docstring.
        next_action = "work"
        # Section 13's ping-pong bound: a SECOND consecutive gate:continue
        # with no real tool executed in between means the model
        # contradicted its own decision -- one contradiction gets a fresh
        # WORK round anyway (benefit of the doubt), a second does not.
        # Deliberately a separate counter from corrective_retry_used
        # above/below -- a different situation from truncation recovery or
        # a malformed gate response, not sharing that budget.
        consecutive_gate_continues = 0

        for iteration in range(config.MAX_TOOL_ITERATIONS):
            if next_action == "gate":
                next_action = "work"
                outcome, gate_error = _run_tool_work_control_gate(
                    self, tools_used_this_turn, cancel_event, reasoning_effort, chat_id,
                    think_step,
                )
                if outcome == "cancelled":
                    raise TurnCancelled()
                if outcome == "error":
                    return gate_error

                if outcome == "finish":
                    # The gate's own ephemeral instruction was already
                    # consumed by its build_messages() call -- build fresh,
                    # clean messages here so it can never leak into the
                    # final stream.
                    clean_messages = self.ctx.build_messages(chat_id=chat_id)
                    return self._stream_final(clean_messages, think_step, cancel_event=cancel_event,
                                               reasoning_effort=reasoning_effort)

                if outcome == "continue":
                    consecutive_gate_continues += 1
                    if consecutive_gate_continues > 1:
                        print(f"[AGENT] tool-work completion gate contradicted its own "
                              f"continue decision twice in a row — visible non-success", flush=True)
                        notice = "[Lumina: tool-work continuation ended without confirming completion.]"
                        on_response_token = getattr(self, "on_response_token", None)
                        if callable(on_response_token):
                            on_response_token(notice)
                        return notice
                    self.ctx.push_ephemeral(
                        "## Tool-work continuation\n"
                        "The completion gate confirmed additional tool work is "
                        "required. Continue by selecting any enabled real tool "
                        "needed to complete the task."
                    )
                    continue

                # "malformed" (both or neither control selected) or
                # "incomplete" (positively truncated) -- bounded retry of
                # the GATE itself, sharing corrective_retry_used's one-shot
                # budget with the ordinary truncation-recovery path below
                # rather than a competing counter. No separate ephemeral
                # push needed here -- _run_tool_work_control_gate() pushes
                # its own instruction fresh on every call it makes
                # (including this retry), and push_ephemeral() OVERWRITES
                # rather than appends, so a second nudge pushed here would
                # only ever be silently clobbered before the model ever
                # saw it.
                if not corrective_retry_used:
                    corrective_retry_used = True
                    print(f"[AGENT] tool-work completion gate ambiguity (outcome={outcome}) — "
                          f"one bounded corrective retry", flush=True)
                    next_action = "gate"
                    continue
                notice = "[Lumina: tool-work continuation ended without confirming completion.]"
                print(f"[AGENT] tool-work completion gate contract violated after "
                      f"corrective retry (outcome={outcome})", flush=True)
                on_response_token = getattr(self, "on_response_token", None)
                if callable(on_response_token):
                    on_response_token(notice)
                return notice

            # ── WORK round: full enabled product profile, AUTO. ──────────
            in_tool_work_phase = bool(tools_used_this_turn)
            tool_schemas = self.registry.get_schemas()  # never the two control primitives
            tool_token_estimate = self.registry.schema_token_estimate()
            messages = self.ctx.build_messages(tool_budget=tool_token_estimate, chat_id=chat_id)
            if _cancel_requested(cancel_event):
                raise TurnCancelled()

            chat_kwargs = dict(
                messages=messages,
                tools=tool_schemas,
                max_tokens=config.RESPONSE_RESERVE_TOKENS,
                reasoning_effort=reasoning_effort,
            )
            if _accepts_tool_choice_mode(self.llm):
                # AGENT-CONTINUATION-CONTROL-GATE-01A -- always AUTO here,
                # every round, work-phase or not. REQUIRED now applies only
                # inside _run_tool_work_control_gate() above/below -- see
                # this method's module-level docstring on
                # _CONTROL_GATE_SCHEMAS for why offering finish_tool_work
                # inside this same full-profile request was replaced.
                chat_kwargs["tool_choice_mode"] = ToolChoiceMode.AUTO

            response, err = _provider_chat_or_error(self, chat_kwargs, cancel_event, tools_used_this_turn)
            if err is not None:
                return err

            if _cancel_requested(cancel_event):
                raise TurnCancelled()
            message = self.llm.extract_message(response)
            # getattr-guarded like every other optional-capability check in
            # this method (see chat()'s own docstring) -- extract_termination
            # is new with AGENT-CONTINUATION-01A. Every real backend has it
            # (concrete BaseLLMBackend default, or an override), but the
            # hand-rolled fake LLM doubles several existing tests construct
            # predate it; UNKNOWN is the correct fallback for those, not a
            # crash, since UNKNOWN never blocks finalization on its own.
            _extract_termination = getattr(self.llm, "extract_termination", None)
            termination = (_extract_termination(response) if callable(_extract_termination)
                           else TerminationStatus.UNKNOWN)
            has_tool_calls = self.llm.is_tool_call(message)
            tool_calls = self.llm.get_tool_calls(message) if has_tool_calls else []

            if not has_tool_calls:
                if not in_tool_work_phase:
                    # Ordinary first-round path -- entirely unchanged: no
                    # tool work has occurred yet, so the completion gate
                    # never applies here (Design Law: silence is not
                    # completion, but an initial genuinely-complete answer
                    # still needs no tool and no gate).
                    if termination != TerminationStatus.INCOMPLETE:
                        return self._stream_final(messages, think_step, cancel_event=cancel_event,
                                                   reasoning_effort=reasoning_effort)

                    if _cancel_requested(cancel_event):
                        raise TurnCancelled()

                    if not corrective_retry_used:
                        corrective_retry_used = True
                        print(f"[AGENT] continuation ambiguity (initial response "
                              f"termination={termination.value}) — one bounded "
                              f"corrective retry", flush=True)
                        self.ctx.push_ephemeral(
                            "## Continuation\n"
                            "Your previous response ended before it was complete. "
                            "Continue: call a tool if one is needed, or give your "
                            "complete final answer now."
                        )
                        continue

                    notice = "[Lumina: response was cut off before it could be confirmed complete.]"
                    print(f"[AGENT] continuation contract violated after corrective retry "
                          f"(in_tool_work_phase=False, termination={termination.value})", flush=True)
                    on_response_token = getattr(self, "on_response_token", None)
                    if callable(on_response_token):
                        on_response_token(notice)
                    return notice

                # In tool work phase, no real tool call this round.
                if termination == TerminationStatus.INCOMPLETE:
                    if _cancel_requested(cancel_event):
                        raise TurnCancelled()

                    if not corrective_retry_used:
                        corrective_retry_used = True
                        print(f"[AGENT] continuation ambiguity (no tool call while tool "
                              f"work is active, termination=incomplete) — one bounded "
                              f"corrective retry", flush=True)
                        self.ctx.push_ephemeral(
                            "## Tool-work continuation\n"
                            "Your previous response ended before it was complete. "
                            "Continue tool work or give your final answer."
                        )
                        continue

                    notice = "[Lumina: tool-work continuation ended without confirming completion.]"
                    print(f"[AGENT] continuation contract violated after corrective retry "
                          f"(in_tool_work_phase=True, termination=incomplete)", flush=True)
                    on_response_token = getattr(self, "on_response_token", None)
                    if callable(on_response_token):
                        on_response_token(notice)
                    return notice

                # COMPLETE/UNKNOWN, in tool work phase, no real tool call:
                # this is NOT completion and NOT ordinary commentary (see
                # this method's module docstring) -- it is an unresolved
                # work-phase response. Whatever content it carries is
                # discarded here; only the completion gate decides whether
                # work is actually done.
                if _cancel_requested(cancel_event):
                    raise TurnCancelled()
                next_action = "gate"
                continue

            # Has one or more real tool calls.
            consecutive_gate_continues = 0  # real work happened -- ping-pong bound resets

            # AGENT-TOOL-THINK-TELEMETRY-01A1 -- collect + emit Think
            # BEFORE strip_think_blocks() below destroys any inline
            # <think> span in message["content"]; this is the one call
            # site that needs the raw, not-yet-stripped content to still
            # be there to read. Required ordering: Think, then a
            # cancellation check, then Commentary, then Tool dispatch (the
            # existing tool-call loop's own first-iteration cancellation
            # check already covers the Commentary-to-Tool-dispatch
            # boundary -- see that loop below).
            reasoning = _collect_tool_round_reasoning(self.llm, response, message)
            _emit_tool_round_think(self, think_step, reasoning)

            if message.get("content"):
                message["content"] = strip_think_blocks(message["content"])

            if _cancel_requested(cancel_event):
                # Cancelled between Think and Commentary: the message
                # (with its tool_calls) still gets persisted and every one
                # of those ids still gets a paired cancelled tool result --
                # identical bookkeeping to the tool-dispatch loop's own
                # first-iteration cancellation check just below, only
                # reached one step earlier so Commentary describing a
                # call that's about to be thrown away never fires. This
                # preserves the EXISTING cancellation contract (see
                # tests/test_operator_stop.py's
                # test_cancel_after_tool_message_before_execution_closes_every_id)
                # rather than weakening it to buy Think/Commentary
                # ordering -- an already-emitted Think event stays
                # emitted (same "truthfully already happened" precedent
                # AGENT-COMMENTARY-01A's own cancellation case establishes
                # for Commentary), it just never gets followed by
                # Commentary for work that never actually happened.
                self.ctx.add_tool_call(message)
                _close_cancelled_tool_calls(self, tool_calls)
                raise TurnCancelled()

            # AGENT-COMMENTARY-01A — read from the already-stripped content
            # that's about to be persisted via add_tool_call() below; this
            # is a UI observation of that real assistant message, not a new
            # synthetic one (see _extract_commentary()/on_commentary
            # docstrings). Emitted once per response, before the tool-call
            # loop below dispatches any of the tools it describes.
            commentary = (message.get("content") or "").strip()
            if commentary:
                on_commentary = getattr(self, "on_commentary", None)
                if callable(on_commentary):
                    on_commentary(commentary)

            self.ctx.add_tool_call(message)

            for index, tc in enumerate(tool_calls):
                if _cancel_requested(cancel_event):
                    _close_cancelled_tool_calls(self, tool_calls[index:])
                    raise TurnCancelled()

                tool_id = tc.get("id", "unknown")
                name, args = self.llm.parse_tool_call(tc)
                if _cancel_requested(cancel_event):
                    _close_cancelled_tool_calls(self, tool_calls[index:])
                    raise TurnCancelled()

                if "web_search" in tools_used_this_turn and name in CHAIN_BLOCKED_AFTER_SEARCH:
                    result = "[Skipped: summarize from search results already provided.]"
                    self.ctx.add_tool_result(tool_id, name, result)
                    if _cancel_requested(cancel_event):
                        _close_cancelled_tool_calls(self, tool_calls[index + 1:])
                        raise TurnCancelled()
                    continue

                self.on_tool_call(name, args)
                if _cancel_requested(cancel_event):
                    _close_cancelled_tool_calls(self, tool_calls[index:])
                    raise TurnCancelled()
                try:
                    result = self.registry.call(name, args)
                except Exception as e:
                    result = f"[Tool error: {name} failed — {e}]"
                    print(f"[TOOL ERROR] {name}: {e}", flush=True)
                self.on_tool_result(name, result)
                tools_used_this_turn.add(name)
                self.ctx.add_tool_result(tool_id, name, result)
                self._session_tool_calls += 1
                
                if _cancel_requested(cancel_event):
                    _close_cancelled_tool_calls(self, tool_calls[index + 1:])
                    raise TurnCancelled()

                # Nudge skill creation after threshold — once per session.
                # FE-26: this used to be ctx.add_user(...), which injected a
                # synthetic USER message that persisted in history forever —
                # every later turn showed "you said" a line the person never
                # typed. push_ephemeral() surfaces the same nudge to the model
                # for its next completion this turn, then it's gone; nothing
                # fake is ever written into the conversation record.
            if (not self._skill_nudge_sent
                    and self._session_tool_calls >= config.SKILLS_TRIGGER_THRESHOLD):
                self._skill_nudge_sent = True
                self.ctx.push_ephemeral(
                    "## Skill reminder\n"
                    "That workflow involved several tool calls. "
                    "If this procedure is reusable, consider calling save_skill() "
                    "to save it for future sessions — before giving your final answer."
                )

        # Max iterations — force final streamed answer
        if _cancel_requested(cancel_event):
            raise TurnCancelled()
        messages = self.ctx.build_messages(chat_id=chat_id)
        messages.append({"role": "user", "content": "Give your final answer now based on what you have."})
        return self._stream_final(messages, think_step, cancel_event=cancel_event,
                                   reasoning_effort=reasoning_effort)

    def _stream_final(self, messages: list, think_step: list, cancel_event=None,
                       reasoning_effort: Optional[str] = None) -> str:
        """Stream the final response, firing callbacks for UI updates."""
        full_response = []
        in_think = False
        stream = None

        def _raise_cancelled():
            if in_think:
                self.on_think_end()
            content = "".join(full_response).strip()
            if content:
                self.ctx.add_assistant(content)
            if stream is not None and hasattr(stream, "close"):
                try:
                    stream.close()
                except Exception:
                    pass
            raise TurnCancelled(content)

        try:
            if _cancel_requested(cancel_event):
                _raise_cancelled()
            stream = iter(self.llm.chat_stream(
                messages=messages,
                max_tokens=config.RESPONSE_RESERVE_TOKENS,
                reasoning_effort=reasoning_effort,
            ))
            while True:
                if _cancel_requested(cancel_event):
                    _raise_cancelled()
                try:
                    chunk = next(stream)
                except StopIteration:
                    break
                if _cancel_requested(cancel_event):
                    _raise_cancelled()
                if chunk == "__THINK_START__":
                    in_think = True
                    think_step[0] += 1
                    self.on_think_start(think_step[0])
                elif chunk == "__THINK_END__":
                    in_think = False
                    self.on_think_end()
                elif in_think:
                    self.on_think_token(chunk)
                else:
                    self.on_response_token(chunk)
                    full_response.append(chunk)

        except (ConnectionError, TimeoutError, RuntimeError, ValueError) as e:
            if _cancel_requested(cancel_event):
                _raise_cancelled()
            # ValueError added alongside Bug C's base_url validation
            # (core/backends/lmstudio.py validate_base_url()) — an
            # unconfigured/schemeless endpoint is a real, expected failure
            # mode here, not a programming bug, and deserves the same
            # graceful "[Stream error: ...]" + persisted-history treatment
            # as the other backend failure types instead of falling through
            # to AgentWorker's cruder top-level handler.
            err = f"[Stream error: {e}]"
            self.on_response_token(err)
            return err

        content = "".join(full_response).strip()
        self.ctx.add_assistant(content)
        if self.tts and content and not getattr(self, "_persona_speech_suppressed", False):
            self.tts.speak(content)
        return content

    def clear_persona_speech_suppression(self):
        """Restore ordinary speech when no persona is active."""
        self._persona_speech_suppressed = False

    def _apply_persona_tts_settings(self, persona: dict):
        """Apply persona audio settings to an already-loaded backend."""
        if not self.tts:
            return
        if "tts_voice" in persona and persona["tts_voice"] is not None:
            if hasattr(self.tts, 'set_profile'):
                self.tts.set_profile(persona["tts_voice"])
            elif hasattr(self.tts, 'set_voice'):
                self.tts.set_voice(persona["tts_voice"])
        if "tts_speed" in persona:
            self.tts.speed = persona["tts_speed"]
        if "tts_pitch" in persona:
            self.tts.pitch = persona["tts_pitch"]
        if "tts_volume" in persona:
            self.tts.volume = persona["tts_volume"]

    def attach_tts_backend(self, backend):
        """Attach a globally enabled backend while preserving persona policy."""
        self.tts = backend
        persona = getattr(self, "current_persona", None)
        if persona is not None:
            self._persona_speech_suppressed = (
                "tts_voice" in persona and persona["tts_voice"] is None
            )
            LuminaAgent._apply_persona_tts_settings(self, persona)
        else:
            self._persona_speech_suppressed = False
    
    def apply_persona(self, persona: dict):
        """Hot-swap agent identity from a persona dict."""
        import config

        # 1. Name
        if "name" in persona:
            config.AGENT_NAME = persona["name"]
            self.persona_avatar = persona.get("avatar")

        # 2. System prompt — global behavior rules FIRST, persona identity
        # layered after. This used to be `new_prompt = persona["system_prompt"]`,
        # a full replace that silently discarded config.SYSTEM_PROMPT (the
        # RESPONSE STYLE / TOOL USE RULES instructions) the instant ANY
        # persona loaded — which happens on every single startup, since
        # main_window loads the last-used persona immediately. The Settings
        # UI label already claimed this prompt "works in conjunction with
        # all Persona prompts"; this is what actually makes that true.
        # Order matters: the operating-discipline rules anchor first, so the
        # model isn't several paragraphs into character voice before hitting
        # them.
        if "system_prompt" in persona:
            self.current_persona = persona  # so Settings can recombine on a live prompt edit
            new_prompt = config.SYSTEM_PROMPT + "\n\n" + persona["system_prompt"]
            self.ctx.update_system_prompt(new_prompt)

        # 3. Tool set — single source of truth, see core/tool_profiles.py.
        # Handles both tools_profile (named) and tools_enabled (inline list);
        # computes against the full raw registry, never the filtered schema list.
        if "tools_profile" in persona or "tools_enabled" in persona:
            from core.tool_profiles import apply_tool_profile
            apply_tool_profile(
                self.registry,
                profile_name=persona.get("tools_profile"),
                tools_enabled=persona.get("tools_enabled"),
                owner=self.owner,
            )

        # 4. Persona-local speech policy, then TTS voice + settings. Missing
        # tts_voice is the legacy voiced behavior; explicit None alone means
        # silent and must never be forwarded to a backend setter.
        self._persona_speech_suppressed = (
            "tts_voice" in persona and persona["tts_voice"] is None
        )
        LuminaAgent._apply_persona_tts_settings(self, persona)

        print(f"[PERSONA] Applied: {persona.get('name', 'unknown')}", flush=True)

    def test_connection(self) -> str:
        ok, msg = self.llm.health_check()
        return msg

    def get_token_count(self) -> int:
        return self.ctx.token_count()

    def get_context_usage(self, chat_id: int = None, refresh: bool = False) -> dict:
        """Return operator-facing context usage with the live tool-schema budget."""
        return self.ctx.context_usage_snapshot(
            tool_budget=self.registry.schema_token_estimate(),
            chat_id=chat_id,
            refresh=refresh,
        )
