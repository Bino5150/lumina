"""
Search Tools — recursive textual source-code search with file:line
locations and surrounding context.

CODING-03A0/A1: additive to tools/filesystem.py's search_files (filename +
optional whole-file substring, no line numbers, no regex, no context) --
that tool is untouched and still exists for filename/discovery use.
search_code is the code-search primitive: exact locations, real context
windows, real regex.

Deliberately stdlib-only (os.walk / re / fnmatch) -- A0's source-vet found
no real ripgrep binary anywhere on the dev machine (only a Claude Code shell
shim and an unrelated app's private copy), no declared dependency in
requirements.txt, and no install step in .github/workflows/tests.yml. An
external `rg` subprocess would silently break on a fresh checkout and in CI.
See A0's report for the full comparison.
"""

import os
import re
import fnmatch

import config
from core.project_context import resolve_project_path

# Excluded by directory NAME, not by a blanket "every dot-directory" rule
# (that's search_files' behavior, proven in A0 to make .github/workflows/
# *.yml -- a legitimate coding artifact -- permanently unsearchable). .git
# is excluded by name specifically, not as a side effect of dot-pruning, so
# other dot-directories (.github, etc.) stay searchable.
EXCLUDED_DIRS = frozenset({".git", "__pycache__", ".venv", "venv", "node_modules"})

# Internal display cap on rendered *matches* (individual matching lines),
# not files or windows. Never exposed in the model-facing schema -- A0's
# design checkpoint deliberately kept this out of the API surface.
MAX_MATCHES = 150

# Reserved headroom (chars) subtracted from config.TOOL_RESULT_MAX_CHARS
# during scanning -- a soft heuristic that keeps the common case from doing
# throwaway work (rendering blocks the footer-fit step would only have to
# drop again). It is NOT the correctness mechanism: _fit_with_footer below
# is what actually guarantees the returned string fits the real, live
# config.TOOL_RESULT_MAX_CHARS ceiling, including at ceilings smaller than
# this reserve itself (CODING-03A1C).
_FOOTER_RESERVE_CHARS = 200


def register_search_tools(registry, project_state=None):
    """project_state: optional core.project_context.ProjectContextState,
    threaded in from the owning LuminaAgent -- identical convention to
    register_filesystem_tools/register_file_edit_tools/register_terminal_tools.
    None (default) preserves resolve_project_path's own no-project legacy
    behavior for any direct/legacy caller."""

    def _is_binary(path: str) -> bool:
        try:
            with open(path, "rb") as f:
                chunk = f.read(8192)
        except OSError:
            return True
        return b"\x00" in chunk

    def _iter_candidate_files(root: str):
        if os.path.isfile(root):
            yield root
            return
        # Sorted at both levels (CODING-03A1C) -- os.walk's own dirnames/
        # filenames order is ambient (filesystem-dependent, not
        # content-dependent), so an unsorted walk made result order and
        # truncation's drop-from-the-tail selection nondeterministic between
        # otherwise-identical calls. dirnames is sorted in place because
        # os.walk reads it back to decide recursion order (top-down mode);
        # sorting after the exclusion filter, not before, means excluded
        # names never influence the comparison.
        for dirpath, dirnames, filenames in os.walk(root):  # followlinks=False (default) left intact
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
            for fn in sorted(filenames):
                yield os.path.join(dirpath, fn)

    def _build_matcher(query: str, regex: bool):
        """Returns (matcher_fn(line) -> bool, error_or_None). Case-sensitive
        in both modes -- deliberate divergence from search_files' hardcoded
        case-insensitive substring check, per CODING-03A0's case-contract
        recommendation. No ignore_case/smart_case parameter."""
        if regex:
            try:
                pattern = re.compile(query)
            except re.error as e:
                return None, f"[Error: invalid regex: {e}]"
            return (lambda line: pattern.search(line) is not None), None
        return (lambda line: query in line), None

    def _merge_windows(match_linenos: list, total_lines: int, context_lines: int):
        """[start, end, [matched line numbers within this window]], 1-based,
        clamped to file bounds. Adjacent/overlapping windows (start <= prev
        end + 1) merge into one -- 'touch' counts as overlap so two matches
        with exactly context_lines between them still merge into a single
        continuous block instead of two blocks sharing a border line."""
        windows = []
        for ln in match_linenos:
            start = max(1, ln - context_lines)
            end = min(total_lines, ln + context_lines)
            if windows and start <= windows[-1][1] + 1:
                windows[-1][1] = max(windows[-1][1], end)
                windows[-1][2].append(ln)
            else:
                windows.append([start, end, [ln]])
        return windows

    def _render_window(rel_display: str, lines: list, start: int, end: int, match_linenos: list) -> str:
        width = len(str(end))
        match_set = set(match_linenos)
        body = []
        for i in range(start, end + 1):
            prefix = ">" if i in match_set else " "
            body.append(f"{prefix} {i:>{width}}: {lines[i - 1]}")
        header = f"{rel_display}:{match_linenos[0]}"
        return header + "\n" + "\n".join(body)

    def _fit_with_footer(blocks: list, block_match_counts: list, total_matches: int, ceiling: int) -> str:
        """Returns a string guaranteed len() <= max(0, ceiling) (CODING-03A1C).
        `blocks`/`block_match_counts` are the scan-phase's already-rendered
        candidate windows, in deterministic file order; this never renders
        new content, only decides how many of those whole windows -- from
        the tail -- can coexist with a truthful `[truncated: showing N of M
        ...]` footer inside the real, live ceiling. N always reflects
        exactly what's still present after any dropping. Degrades through
        shorter explicit markers only when even zero windows leave no room
        for the full counted footer -- never relies on core/context.py's own
        downstream char-slice to enforce the bound."""
        ceiling = max(0, ceiling)
        if ceiling == 0:
            return ""

        n_blocks = len(blocks)
        rendered_matches = sum(block_match_counts)

        while True:
            footer = (f"[truncated: showing {rendered_matches} of {total_matches} "
                      f"matches — narrow with `glob` or a more specific query]")
            if n_blocks:
                candidate = "\n\n".join(blocks[:n_blocks]) + "\n\n" + footer
            else:
                candidate = footer
            if len(candidate) <= ceiling:
                return candidate
            if n_blocks == 0:
                break
            n_blocks -= 1
            rendered_matches -= block_match_counts[n_blocks]

        if len("[truncated]") <= ceiling:
            return "[truncated]"
        if ceiling >= 1:
            return "…"
        return ""

    def search_code(query: str, path: str = ".", glob: str = None,
                     regex: bool = False, context_lines: int = 2) -> str:
        if not isinstance(query, str) or not query.strip():
            return "[Error: query must not be empty or whitespace-only]"

        if isinstance(context_lines, bool) or not isinstance(context_lines, int) or not (0 <= context_lines <= 10):
            return "[Error: context_lines must be an integer between 0 and 10]"

        matcher, err = _build_matcher(query, regex)
        if err:
            return err

        resolved = resolve_project_path(path, project_state)
        if not os.path.exists(resolved):
            return f"[Error: path not found: {resolved}]"

        single_file = os.path.isfile(resolved)
        if single_file:
            if _is_binary(resolved):
                return f"[Skipped: binary file, not searched: {resolved}]"
            display_root = os.path.dirname(resolved) or "."
        else:
            display_root = resolved

        # Read live, not captured at registration time -- config values can
        # change at runtime (backend switch), and a result assembled against
        # a stale budget could either over- or under-fill the real ceiling.
        char_budget = max(0, config.TOOL_RESULT_MAX_CHARS - _FOOTER_RESERVE_CHARS)

        total_matches = 0
        rendered_matches = 0
        rendered_chars = 0
        blocks = []
        block_match_counts = []
        truncated = False

        for filepath in _iter_candidate_files(resolved):
            filename = os.path.basename(filepath)
            # glob applies uniformly, including to an explicit single-file
            # target (CODING-03A1C) -- every supplied constraint must stay
            # meaningful; a caller passing glob="*.md" against a .py target
            # gets the ordinary no-match result, not a silently-ignored
            # filter.
            if glob and not fnmatch.fnmatch(filename, glob):
                continue
            if not single_file and _is_binary(filepath):
                continue

            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue

            # split("\n") on a file ending in "\n" (the near-universal case)
            # manufactures one phantom trailing empty "line" that isn't real
            # content -- strip exactly that one artifact, not genuine blank
            # lines elsewhere (including a genuine final blank line, when
            # the file actually ends "...\n\n").
            lines = text.split("\n")
            if text.endswith("\n"):
                lines.pop()
            match_linenos = [i for i, line in enumerate(lines, 1) if matcher(line)]
            if not match_linenos:
                continue

            total_matches += len(match_linenos)

            # Once truncated, keep scanning (for a truthful total) but stop
            # doing the window/render work -- nothing downstream needs it.
            if truncated:
                continue

            rel_display = os.path.relpath(filepath, display_root)
            windows = _merge_windows(match_linenos, len(lines), context_lines)

            for start, end, win_matches in windows:
                window_match_count = len(win_matches)
                if rendered_matches + window_match_count > MAX_MATCHES:
                    truncated = True
                    break
                block_text = _render_window(rel_display, lines, start, end, win_matches)
                separator_len = 2 if blocks else 0  # "\n\n" between blocks
                prospective_len = rendered_chars + separator_len + len(block_text)
                if prospective_len > char_budget:
                    truncated = True
                    break
                blocks.append(block_text)
                block_match_counts.append(window_match_count)
                rendered_chars = prospective_len
                rendered_matches += window_match_count

        if total_matches == 0:
            return f"[No matches for {query!r} in {resolved}]"

        if not truncated:
            return "\n\n".join(blocks)

        # config.TOOL_RESULT_MAX_CHARS is the real ceiling -- char_budget
        # above is only the scan-phase heuristic. _fit_with_footer enforces
        # the actual invariant against the live value (CODING-03A1C).
        return _fit_with_footer(blocks, block_match_counts, total_matches, config.TOOL_RESULT_MAX_CHARS)

    registry.register(
        name="search_code",
        fn=search_code,
        description="Recursively search source code for a literal string or regex pattern, "
                     "returning exact file:line locations with surrounding source context. "
                     "Use to find definitions/uses of a symbol, TODOs, or any text pattern "
                     "across a directory tree or a single file. Case-sensitive. For "
                     "filename-only lookups, use search_files instead.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Literal text or regex pattern to search for. Case-sensitive."},
                "path": {"type": "string", "description": "File or directory to search. Default current dir (or active Project root)."},
                "glob": {"type": "string", "description": "Optional filename glob to filter matched files, e.g. '*.py'. Applies to a single-file path too: if that file doesn't match, no matches are found."},
                "regex": {"type": "boolean", "description": "If true, treat query as a Python regular expression. Default false (literal substring)."},
                "context_lines": {"type": "integer", "description": "Lines of context shown around each match, 0-10. Default 2."}
            },
            "required": ["query"]
        }
    )
