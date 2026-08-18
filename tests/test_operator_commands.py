from core.operator_commands import (
    chunk_compaction_history,
    command_help,
    compaction_cut_index,
    format_duration,
    format_tokens,
    parse_operator_command,
    persisted_compaction_skip_count,
    unwrap_background_result,
)


def test_non_slash_text_is_not_an_operator_command():
    assert parse_operator_command("hello Lumina") is None


def test_status_parses_case_insensitively_without_argument():
    cmd = parse_operator_command("  /STATUS  ")
    assert cmd.name == "status"
    assert cmd.argument == ""
    assert cmd.known is True


def test_btw_preserves_the_full_side_question():
    cmd = parse_operator_command("/btw why did that test take so long?")
    assert cmd.name == "btw"
    assert cmd.argument == "why did that test take so long?"
    assert cmd.known is True


def test_compact_is_a_known_no_argument_command():
    cmd = parse_operator_command("/COMPACT")
    assert cmd.name == "compact"
    assert cmd.argument == ""
    assert cmd.known is True
    assert "/compact" in command_help()


def test_stop_is_a_known_no_argument_command():
    cmd = parse_operator_command(" /STOP ")
    assert cmd.name == "stop"
    assert cmd.argument == ""
    assert cmd.known is True
    assert "/stop" in command_help()


def test_unknown_slash_command_is_handled_but_not_known():
    cmd = parse_operator_command("/warp now")
    assert cmd.name == "warp"
    assert cmd.argument == "now"
    assert cmd.known is False
    assert "/status" in command_help() and "/btw" in command_help()


def test_operator_formatters_are_compact_and_stable():
    assert format_duration(8) == "8s"
    assert format_duration(65) == "1m 05s"
    assert format_duration(3661) == "1h 01m"
    assert format_tokens(999) == "999"
    assert format_tokens(12_500) == "12.5k"
    assert format_tokens(1_000_000) == "1M"


def test_compaction_cut_keeps_two_newest_user_turns_and_whole_tail():
    history = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "assistant", "content": "tool call", "tool_calls": [{"id": "x"}]},
        {"role": "tool", "content": "tool result"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
    ]
    cut = compaction_cut_index(history)
    assert cut == 6
    assert history[cut]["content"] == "u3"
    assert compaction_cut_index(history[cut:]) is None


def test_persisted_skip_count_tracks_the_same_two_user_turn_boundary():
    persisted = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
    ]
    assert persisted_compaction_skip_count(persisted) == 4


def test_compaction_chunks_replace_attachment_payloads_and_stay_bounded():
    huge_b64 = "A" * 20_000
    history = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{huge_b64}"}},
            {"type": "text", "text": "describe this receipt"},
        ],
    }]
    chunks = chunk_compaction_history(history, max_chars=1000)
    assert chunks == ["user: [image attachment] describe this receipt"]
    assert huge_b64 not in "".join(chunks)
    assert all(len(chunk) <= 1000 for chunk in chunks)


def test_unwrap_background_result_handles_spawn_subagent_shape():
    entry = {
        "status": "success",
        "result": {"success": True, "result": "42", "tool_calls_made": 0, "error": None},
    }
    assert unwrap_background_result(entry) == "42"


def test_unwrap_background_result_surfaces_inner_failure():
    entry = {
        "status": "success",
        "result": {"success": False, "result": "", "tool_calls_made": 0, "error": "boom"},
    }
    assert unwrap_background_result(entry) == "Sidequest failed: boom"
