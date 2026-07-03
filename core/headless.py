"""
Headless turn primitive — instantiate an Agent with no UI, no TTS, no Qt
signals, run one turn, return a structured result. Every comms transport
and subagents build on this.

owner/channel_id have NO safe default — every call site decides them
explicitly. See LUMINA_SECURITY_HARDENING_BLUEPRINT.md Part 3.
"""
import re
import threading
import time
from core.agent import LuminaAgent

# Process-lifetime cache, keyed by channel_id, so a channel can hold an
# actual conversation across messages.
_agents: dict = {}
_last_used: dict = {}   # channel_id -> unix timestamp of last access
_is_owner: dict = {}    # channel_id -> owner bool, tracked for the reaper below
_on_idle_callback = None  # set via set_idle_callback(), fired before a channel is reaped


# Guards the three dicts above. Became necessary once comms/discord_bridge.py
# started running run_headless_turn() via asyncio.to_thread() (S36b — fixing
# a separate bug where a direct blocking call froze the event loop and
# starved Discord's heartbeat). Multiple Discord messages arriving close
# together can now genuinely hit this cache from different threads at once.
# RLock, not Lock — get_headless_agent() calls _reap_idle() internally and
# both need the lock, so it has to be safe to re-acquire from the same
# thread. Deliberately does NOT wrap agent.chat() itself (see
# run_headless_turn below) — only the cache bookkeeping is serialized, not
# the actual LLM call, so this doesn't force channels to queue behind each
# other for inference.
_lock = threading.RLock()

# Idle reaping only targets owner=False agents. Telegram is the one
# owner=True channel today, single instance, low cardinality — no reason
# to make it forget context on a timer. Discord is the actual resource
# risk: every public channel that gets used spins up its own
# ContextManager competing for the same 4GB card as the desktop session.
# An idle one sitting around after everyone's left is pure waste.
IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 min

# Found live (S36, Discord test): a small local model can hallucinate
# tool-call syntax as plain text for a function it was never given (e.g.
# search_memory, which isn't in Discord-Safe). When that happens,
# is_tool_call() never fires — the backend only recognizes a real
# structured tool_calls field, not text that merely resembles one — so
# nothing gets dispatched and registry.call() is never reached. Verified
# directly: calling registry.call("search_memory", ...) on the same
# session returns "[Tool 'search_memory' is currently disabled.]" — a
# second, independent gate that would have caught it even if the first
# one hadn't. So nothing is ever actually exposed by this. But it looks
# broken and vaguely alarming to a real user, and it's a sign the model
# tried to reach past its sandbox — worth stripping before a non-owner
# ever sees it, regardless of how harmless it turned out to be.
_FAKE_TOOL_CALL_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL | re.IGNORECASE)


def _sanitize_response(text: str, owner: bool) -> str:
    if owner or not text:
        return text
    if _FAKE_TOOL_CALL_RE.search(text):
        print("[HEADLESS] Stripped hallucinated tool-call syntax from a "
              "non-owner response — nothing was dispatched, see headless.py "
              "comment for why this is safe but still gets scrubbed.", flush=True)
        cleaned = _FAKE_TOOL_CALL_RE.sub("", text).strip()
        return cleaned or "I don't have access to that here — happy to help with something else!"
    return text


def set_idle_callback(fn):
    """Register a callback fired as (channel_id) right before an idle owner=False
    channel is torn down. Dreaming's Discord-Lite hook attaches here."""
    global _on_idle_callback
    _on_idle_callback = fn
    
def _log_tool_call(channel_id):
        def _fn(name, args):
            print(f"[HEADLESS:{channel_id}] TOOL CALL → {name}({args})", flush=True)
        return _fn


def _log_tool_result(channel_id):
        def _fn(name, result):
            preview = str(result)[:150].replace('\n', ' ')
            print(f"[HEADLESS:{channel_id}] TOOL RESULT ← {name}: {preview}", flush=True)
        return _fn

def _reap_idle():
    with _lock:
        now = time.time()
        stale = [
            cid for cid, ts in _last_used.items()
            if not _is_owner.get(cid, True) and now - ts > IDLE_TIMEOUT_SECONDS
        ]
        for cid in stale:
            if _on_idle_callback:
                _on_idle_callback(cid)
            _agents.pop(cid, None)
            _last_used.pop(cid, None)
            _is_owner.pop(cid, None)


def get_headless_agent(channel_id: str, owner: bool,
                        persona: dict = None,
                        tools_profile: str = None,
                        tools_enabled: list = None,
                        force_tools_profile: str = None) -> LuminaAgent:
    """
    force_tools_profile: applied AFTER persona/tools_profile/tools_enabled,
    on every call - cached agent or freshly constructed, doesn't matter.
    This exists for transports (Discord) where the persona file's identity
    fields (name/avatar/system_prompt) are meant to be user-editable but
    tool access must never come from that same editable file. If a persona
    dict is also passed, its own "tools_profile"/"tools_enabled" keys are
    NOT trusted for tool gating when force_tools_profile is set — only its
    identity fields get applied. See comms/discord_bridge.py.
    """
    with _lock:
        _reap_idle()

        if channel_id not in _agents:
            agent = LuminaAgent(owner=owner, channel_id=channel_id,
                                on_tool_call=_log_tool_call(channel_id),
                                on_tool_result=_log_tool_result(channel_id))
            if persona:
                agent.apply_persona(persona)
            elif tools_profile or tools_enabled:
                from core.tool_profiles import apply_tool_profile
                apply_tool_profile(agent.registry, profile_name=tools_profile,
                                    tools_enabled=tools_enabled, owner=owner)
            _agents[channel_id] = agent
            _is_owner[channel_id] = owner

        _last_used[channel_id] = time.time()
        agent = _agents[channel_id]
        if force_tools_profile:
            from core.tool_profiles import apply_tool_profile
            apply_tool_profile(agent.registry, profile_name=force_tools_profile,
                                tools_enabled=None, owner=owner)
        return agent


def run_headless_turn(task: str, channel_id: str, owner: bool,
                       persona: dict = None, tools_profile: str = None,
                       tools_enabled: list = None,
                       force_tools_profile: str = None) -> dict:
    """Never raises — a bot listener should always get something to relay
    back, even on failure."""
    try:
        agent = get_headless_agent(channel_id, owner, persona=persona,
                                    tools_profile=tools_profile,
                                    tools_enabled=tools_enabled,
                                    force_tools_profile=force_tools_profile)
        # Deliberately outside _lock — agent.chat() is the slow part (LLM
        # inference) and holding the cache lock across it would serialize
        # every channel's conversation behind whichever one is currently
        # generating, which defeats the point of running this in a thread
        # at all. The cache lookup above is the only part that needed
        # protecting.
        source = "OWNER_DIRECT" if owner else "EXTERNAL_CHANNEL_INBOUND"
        response = agent.chat(task, source=source)
        response = _sanitize_response(response, owner)
        return {"success": True, "response": response}
    except Exception as e:
        return {"success": False, "error": str(e)}


def reset_headless_agent(channel_id: str):
    with _lock:
        _agents.pop(channel_id, None)
        _last_used.pop(channel_id, None)
        _is_owner.pop(channel_id, None)