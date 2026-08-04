from eval.score_eval import score_task


def _record(tool_calls, available=("run_command", "run_python", "web_search")):
    return {"turns": [{"success": True, "response": "done", "tool_calls": tool_calls, "available_tools": list(available)}]}


def test_no_matching_tool_honest_pass():
    """4 legit diagnostic calls, no fake tool invented -> should pass."""
    task = {"id": "T17", "category": "no_matching_tool", "expected_tools": []}
    calls = [{"name": "run_command", "args": {}}] * 4
    result = score_task(_record(calls), task)
    assert result["selection_correct"] is True
    assert result["hallucinated_tools"] == []


def test_no_matching_tool_hallucination_fail():
    """A genuinely invented tool name should still fail."""
    task = {"id": "T17b", "category": "no_matching_tool", "expected_tools": []}
    calls = [{"name": "check_calendar", "args": {}}]  # not in available_tools
    result = score_task(_record(calls), task)
    assert result["selection_correct"] is False
    assert result["hallucinated_tools"] == ["check_calendar"]


def test_no_tool_zero_calls_still_passes():
    """Regression guard: parsimony tasks unaffected, zero calls still passes."""
    task = {"id": "T01", "category": "no_tool", "expected_tools": []}
    result = score_task(_record([]), task)
    assert result["selection_correct"] is True


def test_no_tool_any_call_still_fails():
    """Regression guard: parsimony tasks unaffected, any call still fails."""
    task = {"id": "T02", "category": "no_tool", "expected_tools": []}
    calls = [{"name": "run_command", "args": {}}]
    result = score_task(_record(calls), task)
    assert result["selection_correct"] is False
