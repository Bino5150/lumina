"""
vLLM backend — OpenAI-compatible inference server.
High-throughput GPU inference via PagedAttention.
Default port: 8000
Designed for V100+ class hardware.
"""

from .lmstudio import LMStudioBackend


class VLLMBackend(LMStudioBackend):
    """
    vLLM exposes a full OpenAI-compatible API.
    Inherits everything from LMStudioBackend — only difference is default URL.
    """

    name = "vllm"
    display_name = "vLLM"
    default_url = "http://localhost:8000/v1"

    def __init__(self, base_url=None):
        super().__init__(base_url=base_url)
        # vLLM doesn't require an api_key but accepts one — keep header for compat
