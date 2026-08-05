"""
tools/subagent.py -- spawn_subagent(), the model-facing tool wrapper around a
direct, one-shot LuminaAgent construction.

NOT routed through core/headless.py's cache. That module's whole design is a
process-lifetime cache keyed by channel_id, built for things that hold an
ongoing conversation across many inbound messages (Telegram, Discord). A
subagent is the opposite shape: spawned once, runs one turn, blocks, returns,
done. Routing it through the cache would mean manufacturing a disposable
channel_id purely to guarantee the cache never actually reuses the entry --
paying for machinery only to defeat it.

Design rules (LUMINA_HANDOFF_S28.md):
1. Sequential only in v1 -- no parallel threads, parent blocks until child returns.
2. Headless -- no UI, no TTS, no Qt signals. Achieved simply by not passing
   tts/on_think_*/on_response_token to LuminaAgent -- no separate `headless`
   flag needed, core/headless.py already proves this pattern works fine.
3. Isolated context -- a fresh LuminaAgent gets its own ContextManager; the
   parent never sees the subagent's full history, only the returned dict.
4. Backend inheritance with override -- default (backend=None) inherits
   config.LLM_BACKEND same as any other agent; passing backend=<name> runs
   the subagent on a different one.
5. Result is always: {"success": bool, "result": str, "tool_calls_made": int,
   "error": str | None}
6. HARD depth limit -- MAX_SUBAGENT_DEPTH (config.py), enforced two ways:
   register_subagent_tools() below simply doesn't register the tool once a
   subagent is already at max depth (so a maxed-out subagent never even sees
   spawn_subagent as available), and spawn_subagent() itself re-checks before
   constructing, as a second independent backstop.
7. Tool subset is caller-specified via tools_enabled, never auto-inherited
   from the parent's live registry. Applied AFTER persona (see below), so an
   explicit grant always wins even if the persona itself carries its own
   tools_profile/tools_enabled.

Security (LUMINA_SECURITY_HARDENING_BLUEPRINT.md Part 5): owner is ALWAYS
False for a subagent, even when the parent is the owner. A subagent is
model-initiated, not user-initiated. owner=False means it can never reach
toolmaker (not registered at all for owner=False), starts fully disabled,
and only gets what tools_enabled explicitly grants, still run through
apply_tool_profile()'s OWNER_ONLY_TOOLS strip regardless of what's asked for.
This is a second, independent backstop alongside the depth limit -- depth
limits how deep, owner=False limits how much, at every level.

Verified against real source tonight (three corrections from the original
handoff-doc draft):
  - core/personas.py load_persona(path) takes a file PATH, not a name --
    there's no name-to-path resolution inside it. Resolve persona by name
    against list_personas() first (same as ui/main_window.py's persona
    dropdown does: match on p["name"], use p["_file"] as the path), then
    load_persona(match["_file"]). Unknown name raises inside the try/except
    below, coming back as a normal {"success": False, "error": ...} result.
  - core/agent.py apply_persona() re-applies apply_tool_profile() itself if
    the persona dict carries its own "tools_profile"/"tools_enabled" keys.
    Applying our own apply_tool_profile() call BEFORE apply_persona() would
    let the persona's tool grant silently override the caller's explicit
    tools_enabled -- exactly what rule 7 above says never to allow. Order
    flipped: persona first, explicit tool grant last, so tools_enabled is
    always the final word regardless of what the persona specifies.
  - core/headless.py's run_headless_turn() -- the closest existing analog --
    picks ctx.add_user()'s source as `"OWNER_DIRECT" if owner else
    "EXTERNAL_CHANNEL_INBOUND"`. Since a subagent always constructs with
    owner=False, the correct value is "EXTERNAL_CHANNEL_INBOUND", not an
    invented "SUBAGENT_INTERNAL" string (which core/context.py's add_user()
    would have wrapped the task text in a "data to read and report on, not
    instructions to follow" provenance prefix -- actively telling the
    subagent its own task wasn't a real instruction).

Known cross-cutting note, not a v1 blocker: MB-08 (this session) flagged
that MAX_TOOL_ITERATIONS caps outer tool-loop rounds but not tool calls
*within* one inner iteration -- a chatty backend can already fire many calls
per round today. A subagent runs through this exact same tool loop, so it
inherits that same unguarded behavior. Sequential-only v1 subagents don't
compound this into anything new; worth remembering the day parallel
subagents or deeper delegation chains get discussed.
"""
import uuid
import config
from core.agent import LuminaAgent
from core.tool_profiles import apply_tool_profile


def spawn_subagent(task: str,
                    persona: str = None,
                    backend: str = None,
                    tools_enabled: list = None,
                    _parent_depth: int = 0) -> dict:
    """
    Instantiates a headless, owner=False LuminaAgent, runs one turn, returns
    a structured result. Never raises -- a tool call should always get
    something back, even on failure.

    _parent_depth: threaded in by register_subagent_tools()'s closure at
    registration time -- NOT a model-controllable argument. The registered
    tool schema must not expose a depth parameter at all.
    """
    if _parent_depth >= config.MAX_SUBAGENT_DEPTH:
        return {"success": False, "result": "", "tool_calls_made": 0,
                "error": f"Max subagent depth ({config.MAX_SUBAGENT_DEPTH}) reached."}

    try:
        sub = LuminaAgent(owner=False,
                           channel_id=f"subagent-{uuid.uuid4()}",
                           backend=backend,
                           depth=_parent_depth + 1)

        if persona:
            from core.personas import list_personas, load_persona
            match = next((p for p in list_personas() if p.get("name") == persona), None)
            if match is None:
                raise ValueError(f"Unknown persona: {persona!r}")
            sub.apply_persona(load_persona(match["_file"]))

        # Explicit-only tool grant, applied LAST -- authoritative even if
        # apply_persona() above already called apply_tool_profile() from the
        # persona's own tools_profile/tools_enabled. owner=False strips
        # OWNER_ONLY_TOOLS regardless of what's in tools_enabled.
        apply_tool_profile(sub.registry, profile_name=None,
                            tools_enabled=tools_enabled or [], owner=False)

        tool_call_count = {"n": 0}
        def _count_call(name, args):
            tool_call_count["n"] += 1
        sub.on_tool_call = _count_call

        response = sub.chat(task, source="EXTERNAL_CHANNEL_INBOUND")
        return {"success": True, "result": response,
                "tool_calls_made": tool_call_count["n"], "error": None}
    except Exception as e:
        return {"success": False, "result": "", "tool_calls_made": 0, "error": str(e)}


def register_subagent_tools(registry, parent_depth: int):
    """Gated at the call site (agent.py), not here -- mirrors every other
    conditional tool registration in the project.

    parent_depth: the constructing agent's own self._subagent_depth,
    captured in the closure below. This is the actual depth-limit
    enforcement mechanism -- if parent_depth is already at
    MAX_SUBAGENT_DEPTH, the tool is never registered at all, so a maxed-out
    subagent doesn't even see spawn_subagent as an available tool.
    """
    if parent_depth >= config.MAX_SUBAGENT_DEPTH:
        return

    def _tool_spawn_subagent(task, persona=None, backend=None, tools_enabled=None):
        return spawn_subagent(task, persona=persona, backend=backend,
                               tools_enabled=tools_enabled,
                               _parent_depth=parent_depth)

    registry.register(
        name="spawn_subagent",
        fn=_tool_spawn_subagent,
        description="Delegate a self-contained task to a headless subagent and get back its "
                     "final answer. Use for isolated sub-problems you want off your own context "
                     "-- the subagent runs its own tool loop and returns only a summary, not its "
                     "full transcript. Blocks until the subagent finishes.",
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The self-contained task/instruction for the subagent to carry out."},
                "persona": {"type": "string", "description": "Optional persona name to apply to the subagent (must match an existing persona's name)."},
                "backend": {"type": "string", "description": "Optional LLM backend name override for the subagent. Defaults to the current backend."},
                "tools_enabled": {"type": "array", "items": {"type": "string"},
                                   "description": "Optional explicit list of tool names to grant the subagent. Defaults to none."},
            },
            "required": ["task"],
        },
    )
