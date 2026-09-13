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

    # AGENT-CONTINUATION-01B -- live-verified 2026-08-28: a real
    # /chat/completions request against z-ai/glm-5.3-flash with
    # {"tool_choice": "required"} returned HTTP 200 with
    # finish_reason="tool_calls" and a genuine forced tool call (not a
    # transport error, not silently ignored). Inherits LMStudioBackend.chat()
    # unmodified -- this flag alone is what makes that shared chat()
    # actually emit "required" instead of "auto" for this backend.
    supports_required_tool_choice = True

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
        # VISION-TOOL-INTEROP-01 -- sibling per-instance cache, populated by
        # the SAME discover_models() call as _reasoning_cache above (no
        # separate HTTP fetch). Sparse like _reasoning_cache: only models
        # positively confirmed to support vision+tools together get an
        # entry (value always True); a model absent from this dict falls
        # through to supports_vision_with_tools()'s False default via
        # dict.get() -- no distinction between "never discovered" and
        # "discovered but doesn't support the combination".
        self._vision_tool_cache: dict = {}
        # MB-34-LIVE-VISION-TOOL-CAPABILITY-01 -- per-instance hydration
        # ledger for the vision+tools capability cache. The live
        # conversational instance is constructed by loader.get_llm_backend()
        # and, unlike the Settings probe instances, historically NEVER ran
        # discover_models() -- so its cache stayed empty forever and the
        # inherited has_vision transport guard truthfully-but-wrongly
        # dropped tools from every image-bearing request. The repair makes
        # THIS instance establish capability truth itself: the first
        # capability-sensitive consultation (see supports_vision_with_tools)
        # performs exactly ONE bounded discovery attempt per instance
        # lifetime, outcome latched here either way. Fields:
        #   attempted  -- True once this instance has made its single
        #                 hydration attempt. Never reset: another attempt
        #                 requires a new instance (model switch / restart),
        #                 the same recovery path Settings refreshes always
        #                 effectively required. Repeated capability-
        #                 sensitive calls therefore never hammer a down
        #                 provider once per WORK round.
        #   succeeded  -- None until attempted, then the outcome of THAT
        #                 attempt. Externally driven discover_models()
        #                 calls (e.g. refresh_reasoning_capabilities()) do
        #                 not flow through here; capability-state
        #                 reporting reads _reasoning_cache_ready for those,
        #                 so externally established truth is still
        #                 reflected honestly.
        #   diagnostic -- sanitized failure text from the latched attempt
        #                 (display name + HTTP status + exception class
        #                 only -- never keys, headers, or payloads).
        self._vision_tool_discovery: dict = {
            "attempted": False, "succeeded": None, "diagnostic": "",
        }
        # Model ids positively seen in the last successful discovery
        # response on this instance. Lets vision_tool_capability_state()
        # distinguish "discovered and does NOT advertise the combination"
        # (unsupported) from "absent from the discovery response" (unknown)
        # -- UNKNOWN IS NOT UNSUPPORTED. Set by discover_models() alongside
        # the cache replace; getattr-defaulted at read sites so partially
        # constructed test doubles (the established __new__ convention)
        # never AttributeError.
        self._discovered_model_ids: frozenset = frozenset()
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
        new_vision_tool_cache = {}
        for entry in data:
            try:
                if not isinstance(entry, dict):
                    continue
                model_id = entry.get("id")
                if not isinstance(model_id, str) or not model_id:
                    continue
                # Each field's parse is isolated in its own try/except --
                # same "one malformed signal can't take down its sibling"
                # posture _parse_reasoning_metadata() already uses
                # internally for mandatory/supported_efforts.
                try:
                    caps = self._parse_reasoning_metadata(entry)
                    if caps is not None:
                        new_cache[model_id] = caps
                except Exception:
                    pass
                try:
                    if self._parses_vision_tool_capability(entry):
                        new_vision_tool_cache[model_id] = True
                except Exception:
                    pass
            except Exception:
                continue
        self._reasoning_cache = new_cache
        self._vision_tool_cache = new_vision_tool_cache
        # MB-34-LIVE-VISION-TOOL-CAPABILITY-01 -- record which model ids
        # this successful response positively contained, so capability-
        # state reporting can distinguish "seen but lacking the required
        # signals" (unsupported) from "absent from the response" (unknown).
        self._discovered_model_ids = frozenset(models)
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

    @staticmethod
    def _parses_vision_tool_capability(entry: dict) -> bool:
        """
        VISION-TOOL-INTEROP-01 -- True only if THIS model's own OpenRouter
        /models entry positively advertises BOTH signals needed to send
        image content and tools in the same request:

          architecture.input_modalities contains "image" -- the model
          itself accepts image input (not just the OpenRouter route in
          the abstract).

          supported_parameters contains "tools" -- the model accepts
          tool-calling at all on this route.

        Both are OpenRouter's own authoritative, per-model capability
        data -- never inferred from the model id/name/family. A model
        missing either signal (malformed entry, absent/non-dict
        architecture, non-list input_modalities or supported_parameters)
        safely returns False, never True from partial data.

        tool_choice is not checked as a separate third signal: this
        backend's own configured model (z-ai/glm-5.3-flash) advertises
        "tool_choice" in supported_parameters whenever "tools" is present,
        and a real live trial against it (see the campaign report --
        vision_plus_tools_required) confirmed "required" specifically
        works correctly with image content in the same request. A model
        that advertised "tools" without "tool_choice" would still resolve
        through _resolve_tool_choice_mode()'s own existing AUTO fallback
        exactly as it does today for a non-required caller, so omitting
        tools/tool_choice as a compound condition here doesn't relax
        anything _resolve_tool_choice_mode() doesn't already guard.
        """
        architecture = entry.get("architecture")
        input_modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
        has_image_input = isinstance(input_modalities, list) and "image" in input_modalities

        supported_parameters = entry.get("supported_parameters")
        has_tools = isinstance(supported_parameters, list) and "tools" in supported_parameters

        return has_image_input and has_tools

    def supports_vision_with_tools(self, model: Optional[str] = None) -> bool:
        """
        VISION-TOOL-INTEROP-01 override, repaired by
        MB-34-LIVE-VISION-TOOL-CAPABILITY-01 -- answers from the per-instance
        discovery cache, and (the repair) ESTABLISHES that truth on THIS
        instance when it has never been established: if the asked model has
        no cache entry and no successful discovery has ever completed on
        this instance (_reasoning_cache_ready False), exactly ONE bounded
        discover_models() attempt is made here, latched for the instance
        lifetime in _vision_tool_discovery regardless of outcome.

        Previously this method was strictly cache-read-only ("zero HTTP
        here, ever") -- which made the LIVE conversation backend's answer
        depend on whether a throwaway Settings probe happened to run, the
        exact MB-34 defect: the live instance never discovers, so its cache
        stayed empty forever and the inherited has_vision transport guard
        (lmstudio.py chat()) silently dropped tools/tool_choice from every
        image-bearing request. The contract change IS the repair: the
        instance that carries the conversation owns its own capability
        truth.

        Contract after repair:
          model=None -> False, no HTTP (unchanged).
          model in cache -> cached answer, zero HTTP (unchanged).
          model absent + a discovery already succeeded on this instance ->
              cache-miss answer (False) with NO further HTTP: the model was
              positively seen by that response and lacks the required
              signal combination (unsupported), or was absent from it
              (unknown) -- re-discovery cannot change this instance's
              established truth.
          model absent + discovery never succeeded here -> ONE latched
              discover_models() attempt (10s timeout, never raises). On
              success the caches are atomically replaced and the answer
              re-read; on failure the answer stays False (the safest
              established behavior -- never manufacture support from a
              failed discovery) and the sanitized reason is recorded for
              vision_tool_capability_state().

        Populated by the SAME discover_models() HTTP call reasoning-
        capability discovery already performs -- no separate fetch, no
        duplicated network/parsing logic. Settings probes keep using their
        own separate instances; THIS instance's correctness never depends
        on them (a throwaway probe is never required to make the live
        backend true).
        """
        if model is None:
            return False
        if model not in self._vision_tool_cache and not getattr(
            self, "_reasoning_cache_ready", False
        ):
            self._ensure_vision_tool_capability()
        return self._vision_tool_cache.get(model, False)

    def _ensure_vision_tool_capability(self) -> None:
        """
        MB-34-LIVE-VISION-TOOL-CAPABILITY-01 -- the single bounded
        capability-hydration attempt for THIS instance. Called only from
        supports_vision_with_tools() when the asked model has no
        established truth and no successful discovery has ever completed
        here. Exactly one attempt per instance lifetime (latched in
        _vision_tool_discovery regardless of outcome), so repeated
        capability-sensitive calls never re-discover and an unreachable
        OpenRouter is never hammered once per WORK round.

        Reuses discover_models() -- the one established provider-owned
        population path (same HTTP call, same parsing, same atomic cache
        replacement the Settings probes use) -- so there is exactly one
        ownership rule: the instance that carries the conversation
        establishes its own capability truth, at the first consultation
        that needs it, at most once.

        Failure semantics: discover_models() never raises by contract
        (returns a FAILED ModelDiscoveryResult); a raised exception (e.g.
        a test double) is caught and recorded the same way. A failed
        attempt is NEVER converted into a positive capability answer --
        the cache stays empty, supports_vision_with_tools() keeps the
        conservative False, and the sanitized diagnostic is preserved for
        vision_tool_capability_state(). No lock: an instance's chat() runs
        on a single worker thread in this codebase, and the worst case of
        a theoretical race is one duplicate idempotent GET whose atomic
        cache replace preserves correctness.
        """
        meta = getattr(self, "_vision_tool_discovery", None)
        if not isinstance(meta, dict):
            meta = {"attempted": False, "succeeded": None, "diagnostic": ""}
            self._vision_tool_discovery = meta
        if meta.get("attempted"):
            return
        meta["attempted"] = True
        try:
            result = self.discover_models()
        except Exception as exc:  # defensive: discover_models() never raises by contract
            meta["succeeded"] = False
            meta["diagnostic"] = (
                f"{self.display_name} capability discovery attempt failed "
                f"({type(exc).__name__})."
            )
            return
        if result.outcome in (ModelDiscoveryOutcome.SUCCESS, ModelDiscoveryOutcome.EMPTY):
            meta["succeeded"] = True
        else:
            meta["succeeded"] = False
        meta["diagnostic"] = result.diagnostic

    def vision_tool_capability_state(self, model: Optional[str] = None) -> str:
        """
        MB-34-LIVE-VISION-TOOL-CAPABILITY-01 -- truthful capability-state
        reporting for observability and the agent's vision-tool capability
        notice. Returns exactly one of:

          "supported"               -- this model is positively advertised
                                       as vision+tools capable by a
                                       successful discovery on this
                                       instance.
          "unsupported"             -- a successful discovery on this
                                       instance positively saw this model
                                       id, and the model lacks the
                                       required signal combination.
          "unknown_model_absent"    -- a successful discovery completed on
                                       this instance, but this model id
                                       was absent from its response (e.g.
                                       a brand-new route not yet listed).
          "unknown_discovery_failed" -- this instance's single latched
                                       hydration attempt failed; no truth
                                       was established by it.
          "unknown_not_discovered"  -- no discovery has ever succeeded or
                                       been attempted on this instance
                                       (includes model=None).

        UNKNOWN IS NOT UNSUPPORTED: the unknown states mean truth was
        never established, NOT that the model lacks the combination.
        Read-only; zero HTTP; never raises on partially constructed test
        doubles (getattr defaults throughout).
        """
        if model is None:
            return "unknown_not_discovered"
        if model in self._vision_tool_cache:
            return "supported"
        if getattr(self, "_reasoning_cache_ready", False):
            ids = getattr(self, "_discovered_model_ids", None)
            if isinstance(ids, frozenset) and model in ids:
                return "unsupported"
            return "unknown_model_absent"
        meta = getattr(self, "_vision_tool_discovery", None)
        if isinstance(meta, dict) and meta.get("attempted") and meta.get("succeeded") is False:
            return "unknown_discovery_failed"
        return "unknown_not_discovered"

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
