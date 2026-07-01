"""
Headless turn primitive — instantiate an Agent with no UI, no TTS, no Qt
signals, run one turn, return a structured result. Every comms transport
and subagents build on this.

owner/channel_id have NO safe default — every call site decides them
explicitly. See LUMINA_SECURITY_HARDENING_BLUEPRINT.md Part 3.
"""
from core.agent import LuminaAgent

# Process-lifetime cache, keyed by channel_id, so a channel can hold an
# actual conversation across messages. Nothing here tears these down —
# that's the caller's job (idle timeout, explicit reset).
_agents: dict = {}


def get_headless_agent(channel_id: str, owner: bool,
                        persona: dict = None,
                        tools_profile: str = None,
                        tools_enabled: list = None) -> LuminaAgent:
    if channel_id not in _agents:
        agent = LuminaAgent(owner=owner, channel_id=channel_id)
        if persona:
            agent.apply_persona(persona)
        elif tools_profile or tools_enabled:
            from core.tool_profiles import apply_tool_profile
            apply_tool_profile(agent.registry, profile_name=tools_profile,
                                tools_enabled=tools_enabled, owner=owner)
        _agents[channel_id] = agent
    return _agents[channel_id]


def run_headless_turn(task: str, channel_id: str, owner: bool,
                       persona: dict = None, tools_profile: str = None,
                       tools_enabled: list = None) -> dict:
    """Never raises — a bot listener should always get something to relay
    back, even on failure."""
    try:
        agent = get_headless_agent(channel_id, owner, persona=persona,
                                    tools_profile=tools_profile,
                                    tools_enabled=tools_enabled)
        source = "OWNER_DIRECT" if owner else "EXTERNAL_CHANNEL_INBOUND"
        response = agent.chat(task, source=source)
        return {"success": True, "response": response}
    except Exception as e:
        return {"success": False, "error": str(e)}


def reset_headless_agent(channel_id: str):
    _agents.pop(channel_id, None)