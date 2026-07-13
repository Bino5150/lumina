"""
core/backends/omniroute.py

OmniRoute backend — self-hosted OpenAI-compatible AI gateway
(github.com/diegosouzapw/OmniRoute). Routes to 200+ upstream providers
(many free) through a single local endpoint the person runs themselves,
picked up from beta tester nlitend1's ("beta potato") setup.

Deliberately its own independent backend, NOT a subclass of CustomBackend —
the whole point is that "custom" and "omniroute" stay two separately
configured slots (own model, own key), so picking one never clobbers
whatever's saved for the other. Subclasses LMStudioBackend directly instead,
same as Kimi/Qwen/Custom all do — fully OpenAI-compatible wire format,
chat()/chat_stream()/get_model()/list_models()/health_check() all inherited
unmodified. Only __init__ is overridden.

OmniRoute enforces an Authorization header on every request regardless of
value — the exact same quirk discovered and fixed for CustomBackend (any
non-empty bearer token works; it's just checking the header exists). Falls
back to "Bearer lumina" if no key is configured, matching that fix exactly.

default_url matches OmniRoute's own documented local default
(http://localhost:20128/v1) — still fully overridable in Settings for
setups like nlitend1's, where OmniRoute runs on a separate bridged-LAN VM.
"""

from typing import Optional
from .lmstudio import LMStudioBackend
import config


class OmniRouteBackend(LMStudioBackend):

    name = "omniroute"
    display_name = "OmniRoute (self-hosted gateway)"
    default_url = "http://localhost:20128/v1"

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or config.LLM_BACKEND_URL or self.default_url).rstrip("/")
        key = getattr(config, "OMNIROUTE_API_KEY", "").strip()
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}" if key else "Bearer lumina",
        }
        self._model = getattr(config, "OMNIROUTE_DEFAULT_MODEL", "")

    def health_check(self) -> tuple[bool, str]:
        try:
            model = self.get_model()
            return True, f"Connected — {model}"
        except Exception as e:
            return False, f"{e} (is OmniRoute running at {self.base_url}?)"
