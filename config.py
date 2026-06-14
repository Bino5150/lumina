"""
Lumina Configuration
Edit these to match your local setup.
"""
# LuminaAI by Jason 'BINO' Malik - Mo Thugs South - 2026 

# LM Studio
LM_STUDIO_BASE_URL = "http://localhost:1234/v1"
LM_STUDIO_API_KEY = "lm-studio"

# Backend selection — "lmstudio" | "ollama" | "llamacpp"
LLM_BACKEND = "llamacpp"
LLM_BACKEND_URL = "http://localhost:8080/v1"   # None = use backend's default URL
LLAMACPP_DRAFT_MODEL = None  # Path to draft model GGUF for speculative decoding

# ─── Cloud Backend API Keys ───────────────────────────────────────────────────
# Set the key for whichever provider(s) you want to use.
# Leave as empty string to keep the backend disabled.

OPENROUTER_API_KEY = ""
OPENROUTER_DEFAULT_MODEL = "meta-llama/llama-3.1-8b-instruct:free"

DEEPSEEK_API_KEY = ""
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"   # or "deepseek-reasoner" for R1

GROQ_API_KEY = ""
GROQ_DEFAULT_MODEL = "llama-3.3-70b-versatile"

OPENAI_API_KEY = ""
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"


# Model — auto-detect if None
DEFAULT_MODEL = None

# Context management
MAX_CONTEXT_TOKENS = 16000
TOOL_BUDGET_TOKENS = 1500
MEMORY_INJECT_LIMIT = 5
RESPONSE_RESERVE_TOKENS = 4096

# Paths
import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "memory", "lumina.db")
PERSONAS_DIR = os.path.join(BASE_DIR, "personas")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
SKILLS_DIR               = os.path.join(BASE_DIR, "skills")
SKILLS_TRIGGER_THRESHOLD = 5   # tool calls in a session before nudging skill creation
SKILLS_MAX_INJECT        = 2   # max skill docs injected per turn

# TTS — load from prefs if available
def _load_tts_prefs():
    import json
    path = os.path.join(BASE_DIR, "memory", "prefs.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

_p = _load_tts_prefs()
TTS_BACKEND = _p.get("tts_backend", "kokoro")
VOICEBOX_HOST    = _p.get("voicebox_host", "http://localhost:17493")
VOICEBOX_PROFILE = _p.get("voicebox_profile", "Lumina")
VOICEBOX_INSTRUCT = _p.get("voicebox_instruct", "")
CHATTERBOX_HOST      = _p.get("chatterbox_host",     "http://localhost:8004")
CHATTERBOX_VOICE     = _p.get("chatterbox_voice",    "lumina")
CHATTERBOX_REF_DIR   = _p.get("chatterbox_ref_dir",  os.path.join(BASE_DIR, "assets", "voices"))
SUPERTONIC_HOST      = _p.get("supertonic_host",     "http://localhost:7788")
SUPERTONIC_VOICE     = _p.get("supertonic_voice",    "lumina")
TTS_ENABLED = _p.get("tts_enabled", True)
TTS_HOST    = _p.get("tts_host", "http://localhost:8880")
TTS_VOICE   = _p.get("tts_voice", "af_bella")
TTS_SPEED   = _p.get("tts_speed", 1.0)
TTS_PITCH   = _p.get("tts_pitch", 1.0)
TTS_VOLUME  = _p.get("tts_volume", 1.0)

# STT
STT_ENABLED = _p.get("stt_enabled", True)
STT_BACKEND = _p.get("stt_backend", "faster-whisper")
STT_MODEL   = _p.get("stt_model", "base")
STT_DEVICE  = _p.get("stt_device", "cpu")

# Cloud Model
LLM_BACKEND     = _p.get("llm_backend", "llamacpp")
LLM_BACKEND_URL = _p.get("llm_backend_url", "http://localhost:8080/v1")
CUSTOM_DEFAULT_MODEL = _p.get("custom_default_model", "")

del _p

# Agent behavior
AGENT_NAME = "Lumina"
USER_NAME = "User"
MAX_TOOL_ITERATIONS = 20  # Room for complex agentic workflows
TOOL_RESULT_MAX_CHARS = 8000
TOOL_CALL_TIMEOUT = 600  # per-request timeout (resets each tool call)

# System prompt
SYSTEM_PROMPT = """RESPONSE STYLE: Think briefly — 3 to 5 sentences of reasoning max for simple queries. Do not outline, draft, or self-correct in your thinking. Just reason and respond.
TOOL USE RULES:
- Use tools only when they add real value, not for things you already know.
- For web searches: call web_search ONCE, then summarize from the snippets in your response. Do NOT automatically call get_website on results.
- Only call get_website if explicitly asked to.
- Do not retry searches with rephrased queries. One search is enough.
- For normal queries, prefer 1-2 tool calls. For complex agentic workflows, coding tasks, and tool creation, multiple chained calls are acceptable.
- If you feel something is important or worth remembering, create a memory and/or add it to your memory palace.

TOOL WRITING RULES (when using create_tool):
- Every tool file MUST have two things: the tool function, and a register_{name}_tool(registry) function.
- Always use registry.register() with these exact keyword arguments: name, fn, description, parameters.
- Never use registry[name] = fn — this will fail.
- The register function name MUST match: register_{toolname}_tool(registry)
- Example of a correct tool file:

def my_tool(input: str) -> str:
    return f"Result: {input}"

def register_my_tool_tool(registry):
    registry.register(
        name="my_tool",
        fn=my_tool,
        description="One sentence description.",
        parameters={
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "The input string."}
            },
            "required": ["input"]
        }
    )"""