"""
OpenRouter backend — OpenAI-compatible cloud inference aggregator.
Supports hundreds of models from OpenAI, Anthropic, Meta, Google, etc.
Set OPENROUTER_API_KEY in config.py

Patch 3A.4 Part 2B -- dynamic reasoning-capability discovery.

OpenRouter's own /models response can carry a per-model "reasoning" object
(mandatory/default_enabled/supported_efforts/default_effort). Hundreds of
models pass through OpenRouter and that set changes over time, so unlike
every other Part 2A/2B provider in this patch, capability data here is NOT
a hardcoded table -- it's discovered live and cached per-instance. See
_reasoning_cache / discover_models() / reasoning_capabilities() below for
the exact contract: discover_models() performs HTTP, the legacy
list_models() wrapper delegates to it, and reasoning_capabilities() only
reads whatever the last successful structured discovery cached.
"""

from typing import Optional, Generator
from .lmstudio import LMStudioBackend
from .base import ModelDiscoveryOutcome, ModelDiscoveryResult
from .reasoning import ReasoningCapabilities, NO_REASONING_CONTROL
import config


class OpenRouterBackend(LMStudioBackend):

    name = "openrouter"
    display_name = "OpenRouter"
    default_url = "https://openrouter.ai/api/v1"
    endpoint_configurable = False

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = (base_url or self.default_url).rstrip("/")
        self.api_key = getattr(config, "OPENROUTER_API_KEY", "") if api_key is None else api_key
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/mothugs/lumina",   # optional but good practice
            "X-Title": "Lumina",
        }
        self._model = getattr(config, "OPENROUTER_DEFAULT_MODEL", "meta-llama/llama-3.1-8b-instruct:free")
        # Patch 3A.4 Part 2B -- per-instance reasoning-capability cache.
        # Empty at construction time: __init__ does zero network work, same
        # as every other backend, so constructing this class never triggers
        # capability discovery on its own -- only a successful structured
        # discovery call below ever populates this. Keyed by model id string ->
        # ReasoningCapabilities; a model absent from this dict has either
        # never been seen by a successful discovery response, or was seen
        # and had no positively-advertised "reasoning" metadata -- both
        # collapse to NO_REASONING_CONTROL via reasoning_capabilities()'s
        # dict.get() default, no distinction is made between them here.
        self._reasoning_cache: dict = {}
        # Patch 3A.4 Part 4 -- readiness/refresh seam state.
        #
        # _reasoning_cache_ready: True once ANY discover_models() call has ever
        # completed successfully on this instance (i.e. reached the
        # `self._reasoning_cache = new_cache` assignment below), False
        # until then. Never reset back to False by a later failed refresh
        # -- once discovered, this instance stays "discovered" for its
        # whole lifetime, per reasoning_capabilities_ready()'s contract
        # (avoids redundant re-discovery once we already have real data).
        #
        # _last_discovery_succeeded: tracks ONLY the outcome of the most
        # recently completed discover_models() call, regardless of prior
        # history -- this is what lets refresh_reasoning_capabilities()
        # give an honest per-attempt answer even in the (currently
        # unexercised, but not assumed away) case where a LATER refresh
        # fails after an EARLIER one already succeeded: _reasoning_cache_
        # ready would correctly stay True (still discovered from before),
        # while this flag would correctly go False for that specific
        # failed attempt.
        self._reasoning_cache_ready: bool = False
        self._last_discovery_succeeded: bool = False

    def get_model(self) -> str:
        return self._model

    def list_models(self) -> list[str]:
        """Compatibility output: live IDs or the configured-model fallback."""
        result = self.discover_models()
        if result.outcome in (
            ModelDiscoveryOutcome.SUCCESS,
            ModelDiscoveryOutcome.EMPTY,
        ):
            return list(result.models)
        return [self._model]

    def discover_models(self) -> ModelDiscoveryResult:
        """Fetch OpenRouter models and atomically refresh reasoning metadata."""
        import requests
        try:
            resp = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=10)
            raise_for_status = getattr(resp, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()
            elif getattr(resp, "status_code", 200) >= 400:
                raise requests.exceptions.HTTPError()
            payload = resp.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("invalid model-list response structure")
            data = payload["data"]
            if not all(
                isinstance(entry, dict)
                and isinstance(entry.get("id"), str)
                and entry["id"].strip()
                for entry in data
            ):
                raise ValueError("invalid model entry")
            models = [m["id"] for m in data]
        except Exception as exc:
            self._last_discovery_succeeded = False
            status = getattr(locals().get("resp"), "status_code", None)
            suffix = f" (HTTP {status})" if isinstance(status, int) else ""
            return ModelDiscoveryResult(
                ModelDiscoveryOutcome.FAILED,
                diagnostic=(
                    f"{self.display_name} model discovery failed{suffix} "
                    f"({type(exc).__name__})."
                ),
            )

        # Build the new cache fully in a local variable first -- only
        # assign it to self._reasoning_cache after the loop completes, so
        # a crash partway through parsing an individual entry's metadata
        # can never leave the live cache half-built. Each entry also gets
        # its own try/except so one malformed "reasoning" object can't
        # prevent valid sibling entries in the same response from being
        # cached correctly -- this is a full REPLACE of the old cache
        # (never a merge), so a model present in an old cache but absent
        # from this response's model set will not remain cached after
        # this call returns.
        new_cache = {}
        for entry in data:
            try:
                if not isinstance(entry, dict):
                    continue
                model_id = entry.get("id")
                if not isinstance(model_id, str) or not model_id:
                    continue
                caps = self._parse_reasoning_metadata(entry)
                if caps is not None:
                    new_cache[model_id] = caps
            except Exception:
                continue
        self._reasoning_cache = new_cache
        # Patch 3A.4 Part 4 -- atomic alongside the cache replace above:
        # this call reached a full success, so both flags reflect that.
        self._reasoning_cache_ready = True
        self._last_discovery_succeeded = True
        if not models:
            return ModelDiscoveryResult(
                ModelDiscoveryOutcome.EMPTY,
                diagnostic=f"{self.display_name} returned no usable models.",
            )
        return ModelDiscoveryResult(
            ModelDiscoveryOutcome.SUCCESS,
            models=tuple(models),
            diagnostic=f"{self.display_name} returned {len(models)} model(s).",
        )

    @staticmethod
    def _parse_reasoning_metadata(entry: dict) -> Optional[ReasoningCapabilities]:
        """
        Parse one /models entry's optional "reasoning" object into a
        ReasoningCapabilities, or None if the entry has no "reasoning" key
        (or it isn't a dict) -- that None means "no cache entry at all",
        which is how such a model correctly falls through to
        NO_REASONING_CONTROL later via dict.get()'s default.

        Only ever consumes explicitly-present, well-typed fields --
        nothing here is inferred or guessed:

          supported_efforts -> efforts tuple, ONLY if it's a list/tuple of
          non-empty strings, preserving the given order exactly. Anything
          else (missing key, a bare string instead of a sequence, an empty
          list, non-string elements) leaves efforts at the empty-tuple
          default rather than inventing a non-empty tuple from partial
          data.

          default_effort -> set only if it's a non-empty string. Not
          required to be a member of supported_efforts: this field is
          purely informational display metadata per the
          ReasoningCapabilities contract (see reasoning.py's docstring --
          it is explicitly NOT the same thing as the validation-relevant
          `efforts` set), so a provider naming a default outside its own
          enumerated list is still just metadata, not a contract
          violation to reject here.

          mandatory -> set only from an explicit bool (`isinstance(..,
          bool)`), never inferred from presence/absence of anything else.
          A malformed supported_efforts alongside a validly-present
          `mandatory: true` still preserves that mandatory flag with an
          empty efforts tuple -- the two fields are parsed independently.

          default_enabled -> deliberately ignored. ReasoningCapabilities
          has no matching field (only efforts/default_effort/mandatory/
          supports_budget exist), and inventing a new field for it is out
          of scope for this slice.

          supports_budget -> always False for OpenRouter-parsed data:
          OpenRouter's "reasoning" object carries no budget/token-cap
          field (unlike DashScope's thinking_budget), so there is nothing
          positively advertised here to turn this on for.

        A "reasoning" object that is present but yields nothing parseable
        (e.g. {"default_enabled": true} alone) still returns a real,
        all-default ReasoningCapabilities() rather than None -- the model
        did positively advertise a reasoning object, even if nothing
        inside it was usable here, so it earns a (currently inert) cache
        entry rather than being treated identically to a model with no
        "reasoning" key at all.
        """
        reasoning = entry.get("reasoning")
        if not isinstance(reasoning, dict):
            return None

        efforts: tuple = ()
        raw_efforts = reasoning.get("supported_efforts")
        if isinstance(raw_efforts, (list, tuple)) and len(raw_efforts) > 0:
            if all(isinstance(e, str) and e for e in raw_efforts):
                efforts = tuple(raw_efforts)

        default_effort = None
        raw_default = reasoning.get("default_effort")
        if isinstance(raw_default, str) and raw_default:
            default_effort = raw_default

        mandatory = False
        raw_mandatory = reasoning.get("mandatory")
        if isinstance(raw_mandatory, bool):
            mandatory = raw_mandatory

        return ReasoningCapabilities(
            efforts=efforts,
            default_effort=default_effort,
            mandatory=mandatory,
            supports_budget=False,
        )

    def reasoning_capabilities(self, model: Optional[str] = None) -> ReasoningCapabilities:
        """
        Patch 3A.4 Part 2B -- reads the per-instance discovery cache ONLY.
        Zero HTTP here, ever: the cache is populated exclusively by a
        prior successful discover_models() call (see above). `model=None`
        always falls through to NO_REASONING_CONTROL, matching every other
        backend's contract in this patch. An unpopulated cache (discovery
        never run on this instance), an unrecognized model, and a model
        that WAS seen in a successful discovery response but carried no
        positively-advertised "reasoning" metadata all collapse to the
        same NO_REASONING_CONTROL default via dict.get() -- no distinction
        is surfaced between those cases at this layer.
        """
        if model is None:
            return NO_REASONING_CONTROL
        return self._reasoning_cache.get(model, NO_REASONING_CONTROL)

    def reasoning_capabilities_ready(self, model: Optional[str] = None) -> bool:
        """
        Patch 3A.4 Part 4 override -- reports whether ANY discover_models()
        call has ever completed successfully on this instance (see the
        _reasoning_cache_ready docstring in __init__). `model` is accepted
        for interface parity with the base seam but not consulted: OpenRouter
        discovery is a single per-instance cache covering every model in one
        response, not a per-model readiness state.
        """
        return self._reasoning_cache_ready

    def refresh_reasoning_capabilities(self) -> bool:
        """
        Patch 3A.4 Part 4 override -- performs real structured model
        discovery and reports whether THIS specific attempt succeeded via
        _last_discovery_succeeded. It delegates through list_models() for
        compatibility; that wrapper delegates to discover_models(), so the
        request/parsing logic still has one owner. Stays fully separate from
        reasoning_capabilities(), which never performs I/O.
        """
        self.list_models()
        return self._last_discovery_succeeded

    def _apply_reasoning_override(self, payload: dict, effort: str,
                                   model: Optional[str] = None) -> None:
        """
        OpenRouter's own unified `reasoning` object -- payload["reasoning"]
        ["effort"] = effort -- deliberately NOT OpenAI's top-level
        `reasoning_effort` compatibility field that OpenRouter also
        accepts for some routed models; this backend always emits
        OpenRouter's native unified shape. Any pre-existing sibling keys
        already present in payload["reasoning"] (e.g. an "exclude" flag
        set elsewhere) are preserved -- setdefault() only creates the dict
        if it's absent, then only the "effort" key is ever assigned into
        it, same preservation pattern anthropic_backend.py's
        output_config merge uses.
        """
        reasoning = payload.setdefault("reasoning", {})
        reasoning["effort"] = effort

    def _apply_disable_thinking(self, payload: dict) -> None:
        """No local thinking-disable wire fields on OpenRouter's transport.

        UTILITY-RUNTIME-01: OpenRouter forwards requests to hundreds of
        upstream providers; LM Studio's local `thinking` /
        `chat_template_kwargs` fields are not part of its documented
        request contract and at minimum leak through to upstreams that
        reject them. complete_utility()'s assistant-prefill plus its own
        output-side think-strip remain the anti-bleed defense here.
        """
        return None

    def health_check(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "OPENROUTER_API_KEY not set in config.py"
        return True, f"Configured — {self._model}"
