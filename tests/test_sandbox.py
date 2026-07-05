"""F-37: tools/sandbox.py — out-of-process exec, enforced timeout, env
scrubbing, resource limits."""
import os
import time
import pytest
from tools.sandbox import register_sandbox_tools


class _CapturingRegistry:
    def __init__(self):
        self.fns = {}

    def register(self, name, fn, description, parameters):
        self.fns[name] = fn


@pytest.fixture
def run_python(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "BASE_DIR", str(tmp_path))
    reg = _CapturingRegistry()
    register_sandbox_tools(reg)
    return reg.fns["run_python"]


def test_normal_execution(run_python):
    result = run_python("print('hello')")
    assert "hello" in result


def test_exception_captured_not_raised(run_python):
    result = run_python("1/0")
    assert "ZeroDivisionError" in result


def test_no_output_case(run_python):
    result = run_python("x = 1")
    assert result == "[No output]"


def test_timeout_is_actually_enforced(run_python):
    start = time.time()
    result = run_python("import time; time.sleep(10)", timeout=1)
    elapsed = time.time() - start
    assert "timed out" in result
    assert elapsed < 5   # old code had NO enforcement — this would've hung 10s+


def test_timeout_clamped_to_max(run_python):
    # requesting more than MAX_TIMEOUT shouldn't be honored
    from tools.sandbox import MAX_TIMEOUT
    start = time.time()
    result = run_python(f"import time; time.sleep({MAX_TIMEOUT + 20})", timeout=9999)
    elapsed = time.time() - start
    assert elapsed <= MAX_TIMEOUT + 5


def test_secrets_not_inherited_from_app_env(run_python, monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "sk-should-not-leak")
    result = run_python("import os; print(os.environ.get('FAKE_API_KEY', 'NOT_VISIBLE'))")
    assert "NOT_VISIBLE" in result
    assert "sk-should-not-leak" not in result


def test_memory_rlimit_blocks_large_allocation(run_python):
    result = run_python("x = bytearray(2 * 1024**3)")  # 2GB, over the 512MB cap
    assert "MemoryError" in result or "stderr" in result
