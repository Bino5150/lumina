"""
Lumina Agent Loop
Full turn cycle: receive → think → tool calls → stream final response.
"""

import re
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core.backends.loader import get_llm_backend
from core.context import ContextManager
from tools.registry import ToolRegistry
from tools.meta import register_meta_tools
from tools.memory import register_memory_tools, init_memory_db, init_chat_db
from tools.knowledge import register_knowledge_tools
from tools.web import register_web_tools
from tools.filesystem import register_filesystem_tools
from tools.sandbox import register_sandbox_tools
from tools.vision import register_vision_tools
from tools.terminal import register_terminal_tools
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


def _cancel_requested(cancel_event) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


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
                 backend: str = None):
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
        """
        self.llm = get_llm_backend(name=backend)
        self.owner = owner
        self._subagent_depth = depth
        self.ctx = ContextManager(owner=owner)
        self.registry = ToolRegistry()
        self.channel_id = channel_id

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
        register_filesystem_tools(self.registry)
        register_sandbox_tools(self.registry)
        register_vision_tools(self.registry)
        register_terminal_tools(self.registry)
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
        register_projects_tools(self.registry)
        register_diff_tools(self.registry)
        register_browser_tools(self.registry)
        register_telegram_tools(self.registry)
        register_update_tools(self.registry)
        # Promoted from the toolmaker custom-tool pipeline (was approved via
        # create_tool -> approve_pending_tool, then hand-promoted into tracked
        # source once reviewed and hardened) — same as get_weather's history,
        # but these are wired statically here rather than left to the FE-11
        # loader below, since they're now genuine built-ins, not custom tools.
        register_git_status_tool(self.registry)
        register_git_diff_tool(self.registry)
        register_git_log_tool(self.registry)
        register_git_branches_tool(self.registry)

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
            register_subagent_tools(self.registry, self._subagent_depth)

        if config.BACKGROUND_TASKS_ENABLED:
            from tools.tasks import register_task_tools
            register_task_tools(self.registry, self)

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
             cancel_event=None) -> str:
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
        """
        tools_used_this_turn = set()
        think_step = [0]

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
            tool_schemas = self.registry.get_schemas()
            tool_token_estimate = self.registry.schema_token_estimate()
            messages = self.ctx.build_messages(tool_budget=tool_token_estimate, chat_id=chat_id)
            if _cancel_requested(cancel_event):
                raise TurnCancelled()

            try:
                response = self.llm.chat(
                    messages=messages,
                    tools=tool_schemas,
                    max_tokens=config.RESPONSE_RESERVE_TOKENS,
                )
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

            # No tool calls — stream the final response
            if not self.llm.is_tool_call(message):
                return self._stream_final(messages, think_step, cancel_event=cancel_event)

            # Has tool calls
            if message.get("content"):
                message["content"] = strip_think_blocks(message["content"])

            self.ctx.add_tool_call(message)
            tool_calls = self.llm.get_tool_calls(message)

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
        return self._stream_final(messages, think_step, cancel_event=cancel_event)

    def _stream_final(self, messages: list, think_step: list, cancel_event=None) -> str:
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
                max_tokens=config.RESPONSE_RESERVE_TOKENS
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
        if self.tts and content:
            self.tts.speak(content)
        return content
    
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

        # 4. TTS voice + settings
        if self.tts:
            if "tts_voice" in persona:
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
