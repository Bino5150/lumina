"""
Lumina Configuration
Edit these to match your local setup.
"""
# LuminaAI by Jason 'BINO' Malik - Mo Thugs South - 2026 

# LM Studio
LM_STUDIO_BASE_URL = "http://localhost:1234/v1"
LM_STUDIO_API_KEY = "lm-studio"

# Backend selection — "lmstudio" | "ollama" | "llamacpp"
# FE-06: the real, live values are set below (search "Cloud Model"), loaded
# from prefs.json via _p.get() so Settings UI changes actually persist.
# These two lines used to also assign LLM_BACKEND/LLM_BACKEND_URL directly,
# but that assignment always got silently overwritten by the prefs-backed
# one further down — dead code that only cost future readers time figuring
# out which one actually took effect. If you want to hand-edit these without
# touching prefs.json, edit the real ones under "Cloud Model" below instead.
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

ANTHROPIC_API_KEY = ""
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"

GEMINI_API_KEY = ""
GEMINI_DEFAULT_MODEL = "gemini-3.5-flash"

KIMI_API_KEY = ""
KIMI_DEFAULT_MODEL = "kimi-latest"

QWEN_API_KEY = ""
QWEN_DEFAULT_MODEL = "qwen3.5-plus"


# Model — auto-detect if None
DEFAULT_MODEL = None

# Context management — see the "Context management (per-backend)" block below,
# after prefs are loaded and LLM_BACKEND is resolved. Moved there in S41 so
# these can be per-backend and Settings-UI-editable instead of hand-edit-only.

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
# FE-09: same secrets.py-first pattern as the cloud provider keys above.
from core import secrets as _secrets
CUSTOM_API_KEY = _secrets.get_secret("custom_api_key") or _p.get("custom_api_key", "")

# Context management (per-backend) — local backends are hard-capped by
# whatever -c value the server was actually launched with; cloud backends
# have far more real headroom. Each backend remembers its own values in
# prefs["backend_context"][<backend>], written by Settings UI on Save.
# First time a backend is used, falls back to a conservative default below
# rather than crashing or silently reusing another backend's number.
BACKEND_CONTEXT_DEFAULTS = {
    "llamacpp":   {"max_context_tokens": 16384,   "memory_inject_limit": 6},
    "lmstudio":   {"max_context_tokens": 16384,   "memory_inject_limit": 6},
    "ollama":     {"max_context_tokens": 16384,   "memory_inject_limit": 6},
    "vllm":       {"max_context_tokens": 16384,   "memory_inject_limit": 6},
    "custom":     {"max_context_tokens": 16384,   "memory_inject_limit": 6},
    "openrouter": {"max_context_tokens": 32000,   "memory_inject_limit": 12},
    "deepseek":   {"max_context_tokens": 64000,   "memory_inject_limit": 16},
    "groq":       {"max_context_tokens": 32000,   "memory_inject_limit": 12},
    "openai":     {"max_context_tokens": 128000,  "memory_inject_limit": 24},
    "anthropic":  {"max_context_tokens": 180000,  "memory_inject_limit": 40},
    "gemini":     {"max_context_tokens": 1000000, "memory_inject_limit": 60},
    "kimi":       {"max_context_tokens": 128000,  "memory_inject_limit": 24},
    "qwen":       {"max_context_tokens": 128000,  "memory_inject_limit": 24},
}
_ctx_default = BACKEND_CONTEXT_DEFAULTS.get(LLM_BACKEND, BACKEND_CONTEXT_DEFAULTS["llamacpp"])
_backend_ctx = _p.get("backend_context", {}).get(LLM_BACKEND, {})
MAX_CONTEXT_TOKENS  = _backend_ctx.get("max_context_tokens", _ctx_default["max_context_tokens"])
MEMORY_INJECT_LIMIT = _backend_ctx.get("memory_inject_limit", _ctx_default["memory_inject_limit"])

# Not per-backend — tool-call depth and response length are agent-behavior
# choices, not something that varies by which model is answering.
TOOL_BUDGET_TOKENS      = _p.get("tool_budget_tokens", 6000)
RESPONSE_RESERVE_TOKENS = _p.get("response_reserve_tokens", 4096)
MAX_TOOL_ITERATIONS     = _p.get("max_tool_iterations", 20)

# S41 fix: cloud API keys/models saved via Settings UI used to only live in
# the running config module (setattr in ui/settings.py._save()) and silently
# reverted to the hardcoded "" defaults above on every restart — same root
# bug class as MAX_CONTEXT_TOKENS had. The hardcoded defaults above remain a
# valid manual-edit path (comment at top of file still applies); this only
# overrides when Settings UI has actually saved something for that provider.
_cloud_creds = _p.get("cloud_credentials", {})
def _cloud_override(provider: str, key_default: str, model_default: str):
    # FE-09: keys now live in secrets.py (~/.config/lumina/credentials.json),
    # not prefs.json — prefs.json gets dragged into Project uploads and the
    # (genericized) public repo, secrets.py never does. Checks secrets.py
    # first; falls back to whatever's still sitting in prefs.json's
    # cloud_credentials for anyone mid-migration (migrate_legacy_cloud_keys()
    # runs at owner-session startup and moves it over on the next save, but
    # this fallback means nothing breaks on the very first run before that
    # migration has fired).
    saved = _cloud_creds.get(provider, {})
    key = _secrets.get_secret(f"{provider}_api_key") or saved.get("api_key", key_default)
    model = saved.get("default_model", model_default)
    return key, model

OPENROUTER_API_KEY, OPENROUTER_DEFAULT_MODEL = _cloud_override("openrouter", OPENROUTER_API_KEY, OPENROUTER_DEFAULT_MODEL)
DEEPSEEK_API_KEY, DEEPSEEK_DEFAULT_MODEL     = _cloud_override("deepseek", DEEPSEEK_API_KEY, DEEPSEEK_DEFAULT_MODEL)
GROQ_API_KEY, GROQ_DEFAULT_MODEL             = _cloud_override("groq", GROQ_API_KEY, GROQ_DEFAULT_MODEL)
OPENAI_API_KEY, OPENAI_DEFAULT_MODEL         = _cloud_override("openai", OPENAI_API_KEY, OPENAI_DEFAULT_MODEL)
ANTHROPIC_API_KEY, ANTHROPIC_DEFAULT_MODEL   = _cloud_override("anthropic", ANTHROPIC_API_KEY, ANTHROPIC_DEFAULT_MODEL)
GEMINI_API_KEY, GEMINI_DEFAULT_MODEL         = _cloud_override("gemini", GEMINI_API_KEY, GEMINI_DEFAULT_MODEL)
KIMI_API_KEY, KIMI_DEFAULT_MODEL             = _cloud_override("kimi", KIMI_API_KEY, KIMI_DEFAULT_MODEL)
QWEN_API_KEY, QWEN_DEFAULT_MODEL             = _cloud_override("qwen", QWEN_API_KEY, QWEN_DEFAULT_MODEL)

# Dreaming — prefs-backed like everything else in this block, so a Settings
# UI toggle actually survives a restart instead of reverting to these
# hardcoded defaults every time (was defined below `del _p`, so it never
# even had the option to load from prefs before now).
DREAM_SWEEP_ENABLED = _p.get("dream_sweep_enabled", True)
DREAM_MIN_TOKENS    = _p.get("dream_min_tokens", 900)
DREAM_IDLE_MINUTES  = _p.get("dream_idle_minutes", 13)

# Chat UI — show/hide the model's <think> reasoning block in the chat
# window. Purely a display toggle: the model still reasons and those tokens
# still stream in either case, this just controls whether the UI renders
# them. Not the same as actually disabling thinking at the model/backend
# level (a bigger change, deferred — chat_stream() doesn't support
# disable_thinking in any backend yet, only the non-streaming chat() path
# used by complete_utility() does).
SHOW_THINK_BLOCKS = _p.get("show_think_blocks", True)

del _p

# Agent behavior
AGENT_NAME = "Lumina"
USER_NAME = "User"
TOOL_RESULT_MAX_CHARS = 9000
TOOL_CALL_TIMEOUT = 600  # per-request timeout (resets each tool call)
# Telegram bridge — owner identity, not a credential (the bot token lives in
# core/secrets.py instead). None in release; set your real chat ID in OG only.
TELEGRAM_OWNER_CHAT_ID = None

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