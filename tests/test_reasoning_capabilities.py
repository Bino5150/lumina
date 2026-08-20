"""
Patch 3A.4 Part 1 -- backend-level reasoning capability contract.

Covers core/backends/reasoning.py (ReasoningCapabilities, NO_REASONING_CONTROL)
and BaseLLMBackend.reasoning_capabilities() (core/backends/base.py).

This slice introduces the *contract* only -- no real provider wiring yet.
These tests exist to pin down the fail-safe invariant before anything
starts consuming it: no positively advertised capability => no reasoning
override, and unsupported/stale/unknown requests must never be silently
"nearest-matched" to something the caller didn't ask for.
"""
import dataclasses
import pytest

from core.backends.base import BaseLLMBackend
from core.backends.reasoning import ReasoningCapabilities, NO_REASONING_CONTROL


class _NetworkSpyBackend(BaseLLMBackend):
    """
    Minimal concrete test double. Implements only the abstract methods
    BaseLLMBackend actually requires -- deliberately does NOT override
    reasoning_capabilities(), so every test against this class exercises
    the real base-class fail-safe default, not a subclass's behavior.

    list_models() / get_model() raise if called at all, so any test that
    reaches them fails loudly instead of silently passing -- that's the
    "spy" half: proving reasoning_capabilities() never touches them.
    """

    def get_model(self) -> str:
        raise AssertionError("get_model() must not be called by reasoning_capabilities()")

    def list_models(self) -> list:
        raise AssertionError("list_models() must not be called by reasoning_capabilities()")

    def health_check(self):
        raise AssertionError("health_check() must not be called by reasoning_capabilities()")

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024, disable_thinking=False):
        raise AssertionError("chat() must not be called by reasoning_capabilities()")

    def chat_stream(self, messages, max_tokens=1024, temperature=0.7):
        raise AssertionError("chat_stream() must not be called by reasoning_capabilities()")


# ── 1. Base/unknown backend advertises no override capability ──────────

def test_base_backend_default_advertises_no_reasoning_control():
    backend = _NetworkSpyBackend()
    caps = backend.reasoning_capabilities()
    assert caps.efforts == ()
    assert caps.default_effort is None
    assert caps.mandatory is False
    assert caps.supports_budget is False


def test_base_backend_default_is_the_shared_no_reasoning_control_instance():
    """The base default should be the one shared fail-safe sentinel, not
    a fresh equivalent-but-different instance -- callers that special-case
    on identity (e.g. `is NO_REASONING_CONTROL`) get a real answer."""
    backend = _NetworkSpyBackend()
    assert backend.reasoning_capabilities() is NO_REASONING_CONTROL


def test_base_backend_default_ignores_model_argument_and_stays_safe():
    """An unknown/unrecognized model string passed explicitly must still
    collapse to the same safe default -- the base class has no per-model
    data to consult."""
    backend = _NetworkSpyBackend()
    caps = backend.reasoning_capabilities(model="some-totally-unknown-model-xyz")
    assert caps == NO_REASONING_CONTROL


# ── 2 & 3 & 4. validate() semantics ─────────────────────────────────────

def test_none_validates_as_provider_default():
    caps = ReasoningCapabilities(efforts=("low", "medium", "high"))
    assert caps.validate(None) is None


def test_supported_advertised_effort_validates_unchanged():
    caps = ReasoningCapabilities(efforts=("low", "medium", "high"))
    assert caps.validate("medium") == "medium"
    assert caps.validate("low") == "low"
    assert caps.validate("high") == "high"


def test_unsupported_effort_falls_back_to_provider_default():
    caps = ReasoningCapabilities(efforts=("low", "medium", "high"))
    assert caps.validate("ultra-mega-think") is None


def test_stale_effort_no_longer_advertised_falls_back_to_provider_default():
    """A prefs value saved while a model advertised 'xhigh' must fail safe
    once that model (or a switched-to model) no longer advertises it --
    not raise, not silently keep the stale value."""
    caps = ReasoningCapabilities(efforts=("none", "minimal", "low", "medium", "high"))
    assert caps.validate("xhigh") is None


def test_unsupported_effort_never_nearest_matches_to_a_lesser_supported_value():
    """The explicit anti-behavior called out in the spec: 'max' must never
    silently degrade to 'high' just because it's the closest supported
    value. Only an exact match or None is acceptable."""
    caps = ReasoningCapabilities(efforts=("low", "medium", "high"))
    result = caps.validate("max")
    assert result is None
    assert result != "high"


def test_empty_string_and_other_non_none_junk_falls_back_to_provider_default():
    caps = ReasoningCapabilities(efforts=("low", "medium", "high"))
    assert caps.validate("") is None
    assert caps.validate("HIGH") is None  # case must match exactly, no normalization


def test_validate_on_no_reasoning_control_always_falls_back():
    """With an empty efforts tuple (the fail-safe default), every non-None
    request must fail safe -- there is nothing to validate against."""
    assert NO_REASONING_CONTROL.validate(None) is None
    assert NO_REASONING_CONTROL.validate("medium") is None
    assert NO_REASONING_CONTROL.validate("default") is None


# ── 5. Capability order is preserved ────────────────────────────────────

def test_efforts_order_is_preserved_as_given():
    ordered = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    caps = ReasoningCapabilities(efforts=ordered)
    assert caps.efforts == ordered
    assert list(caps.efforts) == list(ordered)


def test_efforts_order_is_not_alphabetized_or_resorted():
    """Deliberately out-of-alphabetical-order input to catch any accidental
    sorting -- dropdown order later must match exactly what was given."""
    weird_order = ("high", "low", "medium")
    caps = ReasoningCapabilities(efforts=weird_order)
    assert caps.efforts == ("high", "low", "medium")


# ── 6. Immutability ──────────────────────────────────────────────────────

def test_capability_object_rejects_attribute_assignment():
    caps = ReasoningCapabilities(efforts=("low", "medium", "high"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.efforts = ("low",)
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.mandatory = True


def test_capability_efforts_sequence_rejects_mutation():
    """efforts is a tuple specifically so it can't be mutated in place even
    though the dataclass field itself can't be reassigned -- a list field
    would still be mutable via .append() despite frozen=True."""
    caps = ReasoningCapabilities(efforts=("low", "medium", "high"))
    assert isinstance(caps.efforts, tuple)
    with pytest.raises(AttributeError):
        caps.efforts.append("max")


def test_no_reasoning_control_sentinel_is_not_accidentally_mutated_by_other_tests():
    """Guards against test pollution: NO_REASONING_CONTROL is a shared
    module-level singleton other code (including the base class default)
    hands out by reference -- it must still read as the safe default."""
    assert NO_REASONING_CONTROL.efforts == ()
    assert NO_REASONING_CONTROL.mandatory is False
    assert NO_REASONING_CONTROL.supports_budget is False


# ── 7. Asking for capabilities triggers no network/discovery call ──────

def test_reasoning_capabilities_never_invokes_list_models_or_get_model():
    """_NetworkSpyBackend's get_model/list_models/health_check/chat/
    chat_stream all raise AssertionError if touched -- reaching this
    assertion at all (rather than an AssertionError bubbling up from one
    of those methods) is the proof that reasoning_capabilities() stayed
    side-effect-free."""
    backend = _NetworkSpyBackend()
    caps = backend.reasoning_capabilities()
    assert caps is NO_REASONING_CONTROL

    caps_with_model = backend.reasoning_capabilities(model="gpt-5")
    assert caps_with_model is NO_REASONING_CONTROL


def test_reasoning_capabilities_side_effect_free_even_called_repeatedly():
    backend = _NetworkSpyBackend()
    for _ in range(5):
        backend.reasoning_capabilities()  # would raise on 1st touch of a spied method


# ── 8. mandatory / supports_budget are independent axes ─────────────────

def test_mandatory_true_without_any_discrete_efforts():
    caps = ReasoningCapabilities(efforts=(), mandatory=True)
    assert caps.mandatory is True
    assert caps.efforts == ()
    # Still fails safe on any concrete request -- nothing to validate against.
    assert caps.validate("high") is None
    assert caps.validate(None) is None


def test_supports_budget_true_without_any_discrete_efforts():
    caps = ReasoningCapabilities(efforts=(), supports_budget=True)
    assert caps.supports_budget is True
    assert caps.efforts == ()


def test_mandatory_and_supports_budget_can_both_be_true_with_efforts_present():
    """The three axes (efforts, mandatory, supports_budget) are fully
    independent -- no combination is disallowed by construction."""
    caps = ReasoningCapabilities(
        efforts=("low", "medium", "high"),
        mandatory=True,
        supports_budget=True,
    )
    assert caps.efforts == ("low", "medium", "high")
    assert caps.mandatory is True
    assert caps.supports_budget is True
    assert caps.validate("low") == "low"


def test_default_effort_is_independent_metadata_not_a_provider_default_sentinel():
    """default_effort is informational (what the provider calls its own
    default) and is completely separate from the request-level `None` /
    future 'default' sentinel semantics -- setting it must not change how
    validate() treats None or any other request value."""
    caps = ReasoningCapabilities(efforts=("low", "medium", "high"), default_effort="medium")
    assert caps.default_effort == "medium"
    # None still means "no override" regardless of what default_effort says.
    assert caps.validate(None) is None
    # default_effort's own value must still validate normally like any
    # other advertised effort -- it isn't magic, it's just metadata.
    assert caps.validate("medium") == "medium"
