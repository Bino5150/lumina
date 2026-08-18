from core.operator_commands import (
    command_help,
    format_duration,
    format_tokens,
    parse_operator_command,
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
