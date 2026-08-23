"""core/backends/gemini_backend.py — Bug B: tool-call continuation used to
fail after the first tool call with Gemini's HTTP 400 "Function call is
missing a thought_signature in functionCall parts." Root cause:
_translate_messages() reconstructed a fresh functionCall part from the
OpenAI-neutral tool_calls record on every continuation request, and that
neutral record (shared by every backend via core/context.py) has no field
for Gemini's opaque thoughtSignature, so it could never be carried forward.

Fix: extract_message() now stashes Gemini's own returned Content object
(unmodified, all parts, in order) on the neutral message dict under
provider_metadata.gemini_content. _translate_messages() re-sends that exact
object on the next turn instead of reconstructing one. Confirmed live against
the real Gemini API before writing these tests (two sequential tool calls,
previously 400'd on the second continuation, now succeeds end-to-end) — this
file covers the translation logic directly, without needing network access.
"""
import json
import pytest
from core.backends.gemini_backend import (
    GeminiBackend,
    normalize_gemini_tool_result,
    _redact_thought_signatures,
    _strip_additional_properties,
)


# ── normalize_gemini_tool_result ────────────────────────────────────────────

def test_normalize_dict_passes_through_unchanged():
    d = {"a": 1, "b": [1, 2]}
    assert normalize_gemini_tool_result(d) is d


@pytest.mark.parametrize("value", [
    [1, 2, 3],
    None,
    "hello",
    42,
    3.14,
    True,
])
def test_normalize_wraps_scalar_and_list_and_none_under_result_key(value):
    assert normalize_gemini_tool_result(value) == {"result": value}


def test_normalize_stringifies_anything_else():
    class Weird:
        def __str__(self):
            return "weird-thing"
    assert normalize_gemini_tool_result(Weird()) == {"result": "weird-thing"}


# ── _redact_thought_signatures ──────────────────────────────────────────────

def test_redact_scrubs_signature_value_only():
    raw = '{"text": "hi", "thoughtSignature": "AbCdEf123secret=="}'
    redacted = _redact_thought_signatures(raw)
    assert "AbCdEf123secret==" not in redacted
    assert '"thoughtSignature": "[REDACTED]"' in redacted
    assert '"text": "hi"' in redacted  # everything else untouched


def test_redact_handles_empty_and_no_match():
    assert _redact_thought_signatures("") == ""
    assert _redact_thought_signatures("no signature here") == "no signature here"


# ── _translate_messages: thoughtSignature preservation ─────────────────────

def _gemini_content_with_call(name, args, call_id_marker, signature=None):
    """Build a Gemini Content object shaped like a real API response."""
    part = {"functionCall": {"name": name, "args": args}}
    if signature is not None:
        part["thoughtSignature"] = signature
    return {"role": "model", "parts": [part], "_marker": call_id_marker}


def test_single_sequential_tool_call_preserves_original_content_object():
    """One tool call, one continuation -- the exact Content Gemini returned
    must be re-sent unmodified, signature included."""
    original_content = _gemini_content_with_call(
        "search_memory", {"query": "launch date"}, "round0", signature="SIG_ROUND_0"
    )
    messages = [
        {"role": "user", "content": "What's the launch date?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "gemini_call_0",
                "type": "function",
                "function": {"name": "search_memory", "arguments": json.dumps({"query": "launch date"})},
            }],
            "provider_metadata": {"gemini_content": original_content},
        },
        {"role": "tool", "tool_call_id": "gemini_call_0", "name": "search_memory",
         "content": "Found it in /tmp/launch_date.txt"},
    ]

    out = GeminiBackend._translate_messages(messages)

    model_turns = [c for c in out if c.get("role") == "model"]
    assert len(model_turns) == 1
    # Must be the SAME object Gemini returned -- not a reconstruction --
    # including the thoughtSignature, which a reconstruction could never have.
    assert model_turns[0] is original_content
    assert model_turns[0]["parts"][0]["thoughtSignature"] == "SIG_ROUND_0"


def test_two_sequential_tool_calls_both_preserve_signatures_in_order():
    """Two separate rounds (search_memory then read_file) -- both Content
    objects, and both signatures, must survive translation in order."""
    content_0 = _gemini_content_with_call(
        "search_memory", {"query": "launch date"}, "round0", signature="SIG_0"
    )
    content_1 = _gemini_content_with_call(
        "read_file", {"path": "/tmp/launch_date.txt"}, "round1", signature="SIG_1"
    )
    messages = [
        {"role": "user", "content": "What's the launch date?"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "gemini_call_0", "type": "function",
            "function": {"name": "search_memory", "arguments": json.dumps({"query": "launch date"})},
        }], "provider_metadata": {"gemini_content": content_0}},
        {"role": "tool", "tool_call_id": "gemini_call_0", "name": "search_memory",
         "content": "Found it in /tmp/launch_date.txt"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "gemini_call_0", "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": "/tmp/launch_date.txt"})},
        }], "provider_metadata": {"gemini_content": content_1}},
        {"role": "tool", "tool_call_id": "gemini_call_0", "name": "read_file",
         "content": "The launch date is 2026-09-01."},
    ]

    out = GeminiBackend._translate_messages(messages)

    model_turns = [c for c in out if c.get("role") == "model"]
    assert len(model_turns) == 2
    assert model_turns[0] is content_0
    assert model_turns[1] is content_1
    assert model_turns[0]["parts"][0]["thoughtSignature"] == "SIG_0"
    assert model_turns[1]["parts"][0]["thoughtSignature"] == "SIG_1"


def test_fallback_reconstruction_when_no_provider_metadata():
    """History without provider_metadata (e.g. predates this fix) must still
    translate without crashing -- degraded (no signature), not broken."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "x", "type": "function",
            "function": {"name": "get_time", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "x", "name": "get_time", "content": "noon"},
    ]
    out = GeminiBackend._translate_messages(messages)
    model_turns = [c for c in out if c.get("role") == "model"]
    assert len(model_turns) == 1
    assert model_turns[0]["parts"][0]["functionCall"]["name"] == "get_time"
    assert "thoughtSignature" not in model_turns[0]["parts"][0]


# ── _translate_messages: parallel tool calls ────────────────────────────────

def test_parallel_tool_calls_preserve_content_as_one_unit_no_signature_copy():
    """Gemini may sign only the FIRST part of a parallel functionCall
    response. The whole Content must move as one unit -- never split and
    reassembled per-call, which risks copying that signature onto the second
    part it was never issued for."""
    parallel_content = {
        "role": "model",
        "parts": [
            {"functionCall": {"name": "search_memory", "args": {"query": "a"}},
             "thoughtSignature": "SIG_ONLY_ON_FIRST"},
            {"functionCall": {"name": "get_time", "args": {}}},  # no signature — realistic
        ],
    }
    messages = [
        {"role": "user", "content": "do two things"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "gemini_call_0", "type": "function",
             "function": {"name": "search_memory", "arguments": json.dumps({"query": "a"})}},
            {"id": "gemini_call_1", "type": "function",
             "function": {"name": "get_time", "arguments": "{}"}},
        ], "provider_metadata": {"gemini_content": parallel_content}},
        {"role": "tool", "tool_call_id": "gemini_call_0", "name": "search_memory", "content": "result A"},
        {"role": "tool", "tool_call_id": "gemini_call_1", "name": "get_time", "content": "noon"},
    ]

    out = GeminiBackend._translate_messages(messages)

    model_turns = [c for c in out if c.get("role") == "model"]
    assert len(model_turns) == 1
    assert model_turns[0] is parallel_content  # untouched, one whole unit
    assert model_turns[0]["parts"][0]["thoughtSignature"] == "SIG_ONLY_ON_FIRST"
    assert "thoughtSignature" not in model_turns[0]["parts"][1]  # never copied onto part 2

    # Both function responses must land in ONE user Content, not two separate
    # back-to-back user turns (FC1,FC2 -> one FR-batch, not FC1,FR1,FC2,FR2).
    assert len(out) == 3  # [user question, model parallel-calls, merged FR batch]
    fr_turn = out[2]
    assert fr_turn["role"] == "user"
    assert len(fr_turn["parts"]) == 2
    names = {p["functionResponse"]["name"] for p in fr_turn["parts"]}
    assert names == {"search_memory", "get_time"}


# ── _strip_additional_properties / _translate_tools (CODING-06A2 corrective 2) ──
# Gemini's function-declaration Schema type does not support
# "additionalProperties" and 400s on a request that includes it. Confirmed by
# source-vetting _translate_tools (it passed fn["parameters"] straight through
# unmodified) and, before this fix, by a hermetic translation of the real
# registered schemas for run_tests / read_coding_checkpoint /
# save_coding_checkpoint -- no live provider/network call involved, here or
# in the fix itself.

def _real_tool_schemas():
    """The actual canonical schemas as ToolRegistry.register() stores them --
    not hand-rolled fakes -- for exactly the three tools the corrective
    named. save_coding_checkpoint is the one with a NESTED
    additionalProperties (inside its "validations" array item schema), not
    just a top-level one."""
    from tools.registry import ToolRegistry
    from tools.tests import register_tests_tools
    from tools.coding_checkpoint import register_coding_checkpoint_tools

    registry = ToolRegistry()
    register_tests_tools(registry, project_state=None, cancel_state=None)
    register_coding_checkpoint_tools(registry, project_state=None)
    return registry, registry.get_schemas()


def _has_additional_properties(node) -> bool:
    if isinstance(node, dict):
        if "additionalProperties" in node:
            return True
        return any(_has_additional_properties(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_additional_properties(item) for item in node)
    return False


@pytest.mark.parametrize("name", ["run_tests", "read_coding_checkpoint", "save_coding_checkpoint"])
def test_gemini_translation_strips_additional_properties(name):
    _registry, schemas = _real_tool_schemas()
    canonical = next(s for s in schemas if s["function"]["name"] == name)
    # Sanity: the canonical registry schema really does carry
    # additionalProperties (this is the condition the fix has to handle) --
    # save_coding_checkpoint's copy is nested two levels deep.
    assert _has_additional_properties(canonical["function"]["parameters"])

    translated = GeminiBackend._translate_tools(schemas)
    declarations = {d["name"]: d for d in translated[0]["functionDeclarations"]}
    assert not _has_additional_properties(declarations[name]["parameters"])


def test_gemini_translation_preserves_other_schema_fields():
    _registry, schemas = _real_tool_schemas()
    canonical = next(s for s in schemas if s["function"]["name"] == "save_coding_checkpoint")
    translated = GeminiBackend._translate_tools(schemas)
    declarations = {d["name"]: d for d in translated[0]["functionDeclarations"]}
    params = declarations["save_coding_checkpoint"]["parameters"]

    assert params["type"] == "object"
    assert params["required"] == canonical["function"]["parameters"]["required"]
    assert params["properties"]["task_id"] == canonical["function"]["parameters"]["properties"]["task_id"]
    validations_items = params["properties"]["validations"]["items"]
    assert validations_items["required"] == ["label", "outcome"]
    assert "enum" in validations_items["properties"]["outcome"]
    assert "additionalProperties" not in validations_items


def test_gemini_translation_never_mutates_canonical_registry_schema():
    """The same schema dict object is shared across every backend's own
    translation via ToolRegistry.get_schemas() -- stripping fields for
    Gemini must never corrupt what OpenAI/Anthropic/the registry itself see
    on a later call."""
    registry, schemas = _real_tool_schemas()
    before = json.dumps(schemas, sort_keys=True)

    GeminiBackend._translate_tools(schemas)

    after = json.dumps(schemas, sort_keys=True)
    assert before == after
    # Re-fetching from the registry (a fresh get_schemas() call) still shows
    # additionalProperties -- proves the stored schema itself, not just this
    # local `schemas` reference, was left untouched.
    refetched = registry.get_schemas()
    save_schema = next(s for s in refetched if s["function"]["name"] == "save_coding_checkpoint")
    assert _has_additional_properties(save_schema["function"]["parameters"])


def test_other_providers_still_receive_additional_properties():
    """Corrective 2 scope: ONLY the Gemini-bound translation strips this
    field. Anthropic's own translator must still see it untouched."""
    from core.backends.anthropic_backend import AnthropicBackend

    _registry, schemas = _real_tool_schemas()
    save_schema = next(s for s in schemas if s["function"]["name"] == "save_coding_checkpoint")

    anthropic_tools = AnthropicBackend._translate_tools([save_schema])
    anthropic_tool = next(t for t in anthropic_tools if t["name"] == "save_coding_checkpoint")
    assert _has_additional_properties(anthropic_tool["input_schema"])


def test_strip_additional_properties_unit_shape():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"label": {"type": "string"}},
                },
            },
        },
    }
    stripped = _strip_additional_properties(schema)
    assert not _has_additional_properties(stripped)
    assert stripped["properties"]["items"]["items"]["properties"]["label"] == {"type": "string"}
    # Original untouched.
    assert _has_additional_properties(schema)
