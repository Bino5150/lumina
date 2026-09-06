"""
Reasoning capability contract -- Patch 3A.4 Part 1.

Small, provider-agnostic representation of what a backend (and, later, a
specific model on that backend) advertises about reasoning/thinking-effort
control. This module intentionally carries NO per-provider data -- it only
defines the shape, plus the one validation rule every later call site
should share instead of hand-rolling its own version of it.

Provider Default semantics (established here, persistence is a later part):
  - `None` on a request means "no reasoning override" -- Lumina sends
    nothing provider-specific and the provider does whatever it does by
    default.
  - A UI/prefs sentinel string (e.g. "default") is a LATER concern for the
    Settings/persistence slice. It must never be treated as, or leak into,
    an actual provider API value from code in this module -- this module
    only ever deals in `None` vs. a real advertised effort string.

The core invariant: no positively advertised capability => no override.
Unknown backend, unknown model, missing metadata, a stale/old install --
all of it collapses safely to Provider Default, never to a guess.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ReasoningCapabilities:
    """
    Immutable description of a backend/model's reasoning-effort surface.

    Fields are independent axes -- none of them implies another:

    efforts: tuple[str, ...]
        Ordered, provider/model-supported discrete effort labels, e.g.
        ("low", "medium", "high") or ("minimal", "low", "medium", "high",
        "xhigh"). Order is meaningful -- it becomes dropdown order in
        Settings later. An empty tuple ("no selectable discrete efforts")
        is the fail-safe default and does NOT imply `mandatory` or
        `supports_budget` are False -- see those fields.
        A tuple (not a list) so the field can't be mutated in place even
        though the dataclass itself is frozen.
    default_effort: Optional[str]
        Purely informational metadata about the provider's own native/
        default effort label, for later UI display. This is NOT the same
        thing as the `None` Provider Default sentinel described above --
        it's just "what the provider calls its default", if known.
    mandatory: bool
        True if the model requires *some* reasoning and can't be turned
        off. Independent of whether `efforts` is non-empty -- a model can
        be mandatory-reasoning without advertising any selectable discrete
        levels (e.g. "always reasons, but effort isn't user-selectable").
    supports_budget: bool
        True if the backend/model supports a numeric reasoning/thinking
        token budget as a control axis, distinct from (or in addition to)
        discrete efforts. Also independent of `efforts`.
    """
    efforts: tuple[str, ...] = ()
    default_effort: Optional[str] = None
    mandatory: bool = False
    supports_budget: bool = False

    def validate(self, requested: Optional[str]) -> Optional[str]:
        """
        The one centralized, boring validation rule -- so later call sites
        (Settings UI, prefs load, request building) don't each hand-roll
        their own version of it.

        - requested is None                 -> None (explicit Provider Default)
        - requested in self.efforts          -> requested, returned unchanged
        - anything else (unsupported, stale,
          unknown, typo'd, from a newer/older
          install, ...)                      -> None (fail safe to Provider
                                                 Default / no override)

        Deliberately does NOT attempt to map an unsupported level to the
        "nearest" supported one -- e.g. a stale "max" saved by an old
        install must never silently become "high" here. Silent
        substitution would change model behavior in a way nothing asked
        for; failing safe to no override is the only safe fallback.
        """
        if requested is None:
            return None
        if requested in self.efforts:
            return requested
        return None

    # UTILITY-RUNTIME-01: canonical ascending-verbosity ladder for the
    # cross-provider effort vocabulary this codebase's own capability
    # tables already use (see gemini_backend.py's ("minimal","low",
    # "medium","high"), openai_backend.py's ("none","low","medium","high",
    # "xhigh","max"), anthropic_backend.py's ("low","medium","high",
    # "xhigh","max")). A pure RANKING table for cheapest_effort() below --
    # never consulted by validate() above, which stays a plain membership
    # check against a specific model's own advertised `efforts`.
    _EFFORT_RANK = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

    def cheapest_effort(self) -> Optional[str]:
        """
        UTILITY-RUNTIME-01: the least-verbose effort this model positively
        advertises, or None if it advertises no ranked effort at all.

        Exists for `mandatory=True` models -- reasoning cannot be turned
        off at all -- where a bounded background utility call still needs
        SOME lever to avoid the provider's own default effort consuming
        its entire output budget before any content token is emitted (see
        BaseLLMBackend._effective_reasoning_effort()'s caller in base.py,
        live-verified against OpenRouter's z-ai/glm-5.3-flash: mandatory=
        true, default_effort="max", and "max" alone reliably exhausts a
        30-token utility budget on reasoning with zero content emitted,
        while "low"/"high" do not).

        Never guesses at an unranked label: a provider-specific effort
        string outside _EFFORT_RANK is excluded from ranking entirely,
        same fail-safe posture as validate() -- an unknown label is never
        silently treated as the cheapest option just because of where it
        happens to sort. A model whose `efforts` contains only unranked
        labels returns None here, same as a model with no efforts at all.
        """
        ranked = [e for e in self.efforts if e in self._EFFORT_RANK]
        if not ranked:
            return None
        return min(ranked, key=self._EFFORT_RANK.index)


# Fail-safe default instance: no positively advertised capability, so
# Lumina sends no reasoning override. Every BaseLLMBackend subclass gets
# this automatically unless it overrides reasoning_capabilities() --
# shared as a single immutable instance since it can't be mutated by a
# caller (frozen dataclass, tuple field).
NO_REASONING_CONTROL = ReasoningCapabilities()
