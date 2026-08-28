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

# AGENT-CONTINUATION-01A — explicit tool-work completion control token.
# Not a product tool: never registered in ToolRegistry, never appears in
# tool inventories/profiles, never reaches registry.call(). Constructed
# locally here and appended to the tool_schemas list _chat_impl() sends to
# the provider ONLY once real tool work has begun this turn (tools_used_
# this_turn non-empty) — see _chat_impl()'s tool loop. Flows through every
# backend's existing extract_message()/is_tool_call()/get_tool_calls()/
# parse_tool_call() unmodified, the same OpenAI-shaped
# {"type":"function","function":{...}} schema every registered tool
# already uses, which is exactly why this needs zero backend-specific code
# to work identically across the OpenAI-compatible family, Anthropic, and
# Gemini.
FINISH_TOOL_WORK_NAME = "finish_tool_work"

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


def _extract_finish_tool_work(llm, tool_calls: list):
    """Return the finish_tool_work tool_call dict if present in
    `tool_calls`, else None. Goes through the backend's own
    parse_tool_call() rather than name-matching a raw provider-specific
    shape, so this works identically across every backend family."""
    for tc in tool_calls:
        try:
            name, _ = llm.parse_tool_call(tc)
        except Exception:
            continue
        if name == FINISH_TOOL_WORK_NAME:
            return tc
    return None


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


def strip_think_blocks(text: str) -> str:
    if not text:
        return text
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()


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

        for iteration in range(config.MAX_TOOL_ITERATIONS):
            in_tool_work_phase = bool(tools_used_this_turn)
            tool_schemas = self.registry.get_schemas()
            if in_tool_work_phase:
                # See FINISH_TOOL_WORK_SCHEMA docstring above — only offered
                # once real tool work has actually begun this turn. An
                # ordinary first-round answer that needs no tool never sees
                # this and is never required to call it.
                tool_schemas = tool_schemas + [_FINISH_TOOL_WORK_SCHEMA]
            tool_token_estimate = self.registry.schema_token_estimate()
            messages = self.ctx.build_messages(tool_budget=tool_token_estimate, chat_id=chat_id)
            if _cancel_requested(cancel_event):
                raise TurnCancelled()

            # AGENT-CONTINUATION-01B — REQUIRED once tool work is under way,
            # so a backend with live-verified support (see
            # supports_required_tool_choice overrides) can structurally
            # rule out the prose-only continuation live acceptance actually
            # observed from GLM in 01A, instead of relying on prompting
            # alone. Never inspected by this method afterwards — a backend
            # that doesn't support it silently falls back to AUTO
            # (_resolve_tool_choice_mode), and the existing ambiguous/
            # corrective-retry/terminal-notice logic below is UNCHANGED and
            # remains the safety net either way (section 12: a supported
            # backend that still returns no tool call is a genuine contract
            # violation, not something to paper over with new logic here).
            chat_kwargs = dict(
                messages=messages,
                tools=tool_schemas,
                max_tokens=config.RESPONSE_RESERVE_TOKENS,
                reasoning_effort=reasoning_effort,
            )
            if _accepts_tool_choice_mode(self.llm):
                chat_kwargs["tool_choice_mode"] = (
                    ToolChoiceMode.REQUIRED if in_tool_work_phase else ToolChoiceMode.AUTO
                )

            try:
                response = self.llm.chat(**chat_kwargs)
            except Exception as e:
                if _cancel_requested(cancel_event):
                    raise TurnCancelled()
                provider = getattr(self.llm, "display_name", None) or getattr(self.llm, "name", "the provider")
                get_model = getattr(self.llm, "get_model", None)
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
                    # uses below) rather than just returned, because agent.chat()
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
                on_response_token = getattr(self, "on_response_token", None)
                if callable(on_response_token):
                    on_response_token(err)
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

            # AGENT-CONTINUATION-01A — explicit completion signal. The
            # response containing it is discarded exactly like today's
            # ordinary "no tool_calls" final case below (never persisted to
            # ctx, never dispatched through registry.call()) and the turn
            # proceeds straight into the same _stream_final() regeneration
            # pass every final turn already goes through — see module
            # docstring on _FINISH_TOOL_WORK_SCHEMA for why chat_stream()
            # itself is deliberately left tool-less (section 7 of the
            # continuation-contract design: that boundary stays put). Any
            # real tool_calls present in the same response are deliberately
            # NOT executed — the model declared tool work complete in the
            # same breath, so nothing runs "after" that declaration.
            finished_call = _extract_finish_tool_work(self.llm, tool_calls)
            if finished_call is not None:
                return self._stream_final(messages, think_step, cancel_event=cancel_event,
                                           reasoning_effort=reasoning_effort)

            if not has_tool_calls:
                # No real tool call and no explicit completion signal. Old
                # rule (implicit "no tool_calls == finished") only still
                # applies to a genuine first-round answer that is positively
                # NOT truncated — Design Law: silence is not completion.
                ambiguous = in_tool_work_phase or termination == TerminationStatus.INCOMPLETE
                if not ambiguous:
                    return self._stream_final(messages, think_step, cancel_event=cancel_event,
                                               reasoning_effort=reasoning_effort)

                if _cancel_requested(cancel_event):
                    raise TurnCancelled()

                if not corrective_retry_used:
                    corrective_retry_used = True
                    if in_tool_work_phase:
                        reason = "no tool call or finish_tool_work while tool work is active"
                        nudge = (
                            "## Tool-work continuation\n"
                            "Tool work is active this turn. Either call another tool if "
                            "work remains, or call finish_tool_work if tool work is "
                            "complete and you are ready to give your final answer."
                        )
                    else:
                        reason = f"initial response termination={termination.value}"
                        nudge = (
                            "## Continuation\n"
                            "Your previous response ended before it was complete. "
                            "Continue: call a tool if one is needed, or give your "
                            "complete final answer now."
                        )
                    print(f"[AGENT] continuation ambiguity ({reason}) — "
                          f"one bounded corrective retry", flush=True)
                    self.ctx.push_ephemeral(nudge)
                    continue

                # Corrective retry already spent this turn — never launder
                # this into a silent final answer. Same non-persistence
                # convention as the provider-continuation-failure path
                # above: surfaced live via on_response_token, not written
                # into ctx history as if it were genuine assistant content
                # (see AGENT-CONTINUATION-01A section 9/10 — this is a
                # distinct condition from a raised provider exception and
                # must stay visibly distinguishable from one).
                if in_tool_work_phase:
                    notice = "[Lumina: tool-work continuation ended without confirming completion.]"
                else:
                    notice = "[Lumina: response was cut off before it could be confirmed complete.]"
                print(f"[AGENT] continuation contract violated after corrective retry "
                      f"(in_tool_work_phase={in_tool_work_phase}, "
                      f"termination={termination.value})", flush=True)
                on_response_token = getattr(self, "on_response_token", None)
                if callable(on_response_token):
                    on_response_token(notice)
                return notice

            # Has one or more real tool calls (finish_tool_work already
            # ruled out above).
            if message.get("content"):
                message["content"] = strip_think_blocks(message["content"])

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
