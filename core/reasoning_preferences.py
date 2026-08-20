"""
core/reasoning_preferences.py -- Patch 3A.4 Part 4.

Persist and restore reasoning-effort selection independently per
backend+model, reusing core/persistence.py's existing prefs.json rather
than creating a second JSON file.

Persisted JSON shape -- a new top-level "backend_reasoning" key inside the
same prefs dict core/persistence.py already reads/writes:

    prefs["backend_reasoning"][backend_name][model_id] = value

backend_name: a backend's own canonical `name` class attribute (e.g.
"openai", "anthropic", "gemini", "groq", "openrouter", "qwen", "omniroute",
"lmstudio", "ollama", "custom", ...) -- never a display_name, never
re-derived by string munging. Every BaseLLMBackend subclass has one
(confirmed by grep across core/backends/*.py + loader.py's CustomBackend).

model_id: stored EXACTLY as the backend's own configured_model() /
provider-facing model string spells it -- no lowercasing, no other
normalization. Callers may trim accidental *surrounding* whitespace before
calling set_saved_reasoning(), but this module does not rewrite identity
itself.

value: one of
    - None (JSON null) -- Lumina's own "Provider Default" sentinel. Means
      "send no reasoning override for this backend+model." A missing
      backend key, a missing model key, and an explicit stored null all
      currently resolve to this same runtime behavior via
      get_saved_reasoning() -- but they remain distinctly representable in
      the underlying dict (an explicit null is a real, intentional choice
      a future Settings UI can write, distinct from "this model has never
      been touched").
    - any other string -- an arbitrary, provider-advertised effort label.
      Never validated here -- validation against a backend's *current*
      capability data happens only inside resolve_reasoning_effort(),
      never in the pure get_saved_reasoning()/set_saved_reasoning() dict
      helpers below.

Critical distinction that must never blur: the JSON value `null` (Python
`None`) is Lumina's Provider-Default sentinel and NEVER reaches a
provider's wire request. The STRING `"default"` is a REAL, distinct,
provider-native effort literal for Groq's Qwen-on-Groq models (see
core/backends/groq.py's _QWEN_REASONING_CAPS = ReasoningCapabilities(
efforts=("none", "default"))) -- when saved and still valid, it is treated
like any other advertised effort string and CAN reach the wire. See
tests/test_reasoning_preferences.py's dedicated collision regression.

No global enum of effort values lives here or anywhere else in this
patch -- efforts are arbitrary, provider-advertised strings, validated
dynamically via each backend's own reasoning_capabilities().validate().
"""
from typing import Optional

import core.persistence as persistence

_PREFS_KEY = "backend_reasoning"


def get_saved_reasoning(prefs: dict, backend_name: str, model: str) -> Optional[str]:
    """
    Pure dict read -- no I/O, no capability validation (that's a separate
    runtime-resolution step; see resolve_reasoning_effort() below).

    Missing "backend_reasoning" key, missing backend_name, missing model,
    and an explicit stored `null` all return None (Provider Default).
    """
    backend_map = prefs.get(_PREFS_KEY)
    if not isinstance(backend_map, dict):
        return None
    model_map = backend_map.get(backend_name)
    if not isinstance(model_map, dict):
        return None
    return model_map.get(model)


def set_saved_reasoning(prefs: dict, backend_name: str, model: str,
                         value: Optional[str]) -> dict:
    """
    Mutates `prefs` in place -- creating prefs["backend_reasoning"] and
    prefs["backend_reasoning"][backend_name] as needed -- and also returns
    the same dict for convenient chaining.

    Deliberately does NOT call persistence.save() itself. Intended usage:

        prefs = persistence.load()
        set_saved_reasoning(prefs, backend.name, model, value)
        persistence.save(prefs)

    This lets a future Settings UI (Part 5, not this slice) bundle a
    reasoning-effort change together with everything else it's already
    saving into one atomic write, instead of this module racing its own
    separate save against Settings' save.

    `value=None` is a legitimate, explicit call (writes a real JSON
    `null`) -- distinct from never having called this for that
    backend/model at all.
    """
    backend_map = prefs.setdefault(_PREFS_KEY, {})
    model_map = backend_map.setdefault(backend_name, {})
    model_map[model] = value
    return prefs


def resolve_reasoning_effort(backend, prefs: Optional[dict] = None,
                              model: Optional[str] = None) -> Optional[str]:
    """
    Runtime resolver -- the one real-logic function in this module.
    Combines a saved preference with the backend's LIVE capability data
    (which, for OpenRouter, may require one explicit discovery refresh) to
    produce the reasoning_effort value a caller should pass into
    LuminaAgent.chat() for this turn.

    prefs: an in-memory prefs dict (e.g. already loaded and possibly
    mutated by set_saved_reasoning() elsewhere in the same call chain). If
    omitted (None), this function calls persistence.load() itself -- this
    is what makes cold-start restoration work (CLI/headless/GUI startup,
    or any fresh turn) without requiring a caller to thread a prefs dict
    through first. Passing an explicit dict is preferred whenever one is
    already in hand (testability, chaining with set_saved_reasoning()).

    model: the model id to resolve against. If omitted (None), resolved
    via backend.configured_model() -- a side-effect-free, network-free
    read (see BaseLLMBackend.configured_model()). If that also comes back
    None (backend has no configured model, e.g. an unconfigured
    OmniRouteBackend), this function returns None immediately -- there is
    nothing to look up a saved preference for.

    Exact required sequence (Patch 3A.4 Part 4 spec section 10):
      1. saved = get_saved_reasoning(...). If saved is None -> return None
         immediately, WITHOUT ever calling refresh_reasoning_capabilities()
         -- discovery is never performed merely to prove Provider Default
         is valid (it always is).
      2. If backend.reasoning_capabilities_ready(model) is already True
         (static providers are always ready; OpenRouter may already have
         discovered this session) -> skip straight to validation, no
         refresh call at all.
      3. Otherwise (not ready -- only possible for OpenRouter pre-discovery)
         -> call backend.refresh_reasoning_capabilities() exactly once.
         If it reports failure -> return None safely.
      4. Validate: caps = backend.reasoning_capabilities(model);
         return caps.validate(saved) -- None if saved is stale/unsupported
         under CURRENT capability data (deliberately no "nearest match"
         degradation -- see core/backends/reasoning.py).

    Never mutates the saved preference in any branch above -- a validation
    failure or a refresh failure both just resolve to None for THIS call;
    the stored prefs value is left exactly as it was for a human (a future
    Settings UI) to eventually deal with.

    Does NOT substitute caps.default_effort when saved is None -- Provider
    Default means "send no override," never "force the provider's own
    documented default effort explicitly."
    """
    if prefs is None:
        prefs = persistence.load()

    if model is None:
        model = backend.configured_model()
    if model is None:
        return None

    saved = get_saved_reasoning(prefs, backend.name, model)
    if saved is None:
        return None

    if not backend.reasoning_capabilities_ready(model):
        if not backend.refresh_reasoning_capabilities():
            return None

    caps = backend.reasoning_capabilities(model)
    return caps.validate(saved)
