"""
llama.cpp server backend — OpenAI-compatible endpoint.
Default port: 8080
Adds draft_model_path support for speculative decoding.
"""

from .lmstudio import LMStudioBackend
import config


class LlamaCppBackend(LMStudioBackend):
    """
    llama.cpp server exposes the same OpenAI-compat API as LM Studio.
    Inherits everything from LMStudioBackend — only differences are
    the default URL, display name, and draft model awareness.
    """

    name = "llamacpp"
    display_name = "llama.cpp"
    default_url = "http://localhost:8080/v1"

    def __init__(self, base_url=None, draft_model_path=None):
        super().__init__(base_url=base_url or config.LLM_BACKEND_URL)
        self.base_url = (base_url or config.LLM_BACKEND_URL or self.default_url).rstrip("/")
        # draft_model_path is informational for now — passed to llama-server via
        # launch args (-md), not the HTTP API. Stored here for the settings UI to read.
        self.draft_model_path = draft_model_path or getattr(config, "LLAMACPP_DRAFT_MODEL", None)