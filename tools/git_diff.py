"""Tool: git_diff - show diffs from a commit or between refs in a local repo."""
import subprocess
import os
from typing import Optional

from core.git_repos import resolve_git_repo
from core.project_context import ProjectContextState


def git_diff(repo_path: Optional[str] = None, commit: str = None, ref_a: str = None, ref_b: str = None,
             max_lines: int = 300, *, project_state: Optional[ProjectContextState] = None) -> str:
    """
    Show diff for a specific commit or between two refs in a local git repo.

    Args:
        repo_path: Absolute or ~-expanded path to a git repository. If omitted, defaults to
            the active Project's root (still subject to the Git allowlist).
        commit: Show the diff introduced by this commit (single commit hash/ref).
        ref_a: Start of range (branch/commit/tag). Requires ref_b.
        ref_b: End of range (branch/commit/tag). Requires ref_a.
        max_lines: Maximum diff lines to return (default 300, max 2000).
        project_state: internal — the calling agent's ProjectContextState, injected by the registrar.

    Returns:
        Unified diff output or error message.
    """
    repo_path, _err = resolve_git_repo(repo_path, project_state)
    if _err:
        return _err
    if not os.path.isdir(repo_path):
        return f"Error: path not found: {repo_path}"
    if not os.path.isdir(os.path.join(repo_path, '.git')):
        return f"Error: not a git repository: {repo_path}"

    if commit and (ref_a or ref_b):
        return "Error: use either 'commit' OR 'ref_a+ref_b', not both."
    if bool(ref_a is not None) != bool(ref_b is not None):
        return "Error: ref_a and ref_b must be used together."

    if commit:
        # Show what this single commit introduced. --end-of-options guards
        # against a commit value like "--output=/some/path" being parsed as
        # a git flag instead of a revision (argument-injection hardening).
        cmd = ['git', 'diff', '--end-of-options', f'{commit}^..{commit}']
    elif ref_a and ref_b:
        cmd = ['git', 'diff', '--end-of-options', ref_a, ref_b]
    else:
        # Default: show unstaged diff (working tree vs index)
        cmd = ['git', 'diff']
        if not os.path.exists(os.path.join(repo_path, '.git')):
            return "Error: not a git repository."
    
    try:
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return f"Git error:\n{result.stderr.strip()}"
        
        output = result.stdout
        if not output.strip():
            if commit:
                return f"No diff — commit {commit} may be the initial commit (no parent), or has no file changes."
            else:
                return "No differences — working tree is clean."
        
        # Truncate if needed
        max_lines = min(max(max_lines, 10), 2000)
        lines = output.split('\n')
        if len(lines) > max_lines:
            shown = lines[:max_lines]
            truncated = len(lines) - max_lines
            output = '\n'.join(shown)
            output += f'\n\n... ({truncated} more lines truncated. Use a narrower range or increase max_lines.)'
        
        # Summarize what files changed
        files_changed = [l for l in lines if l.startswith('diff --git')]
        file_count = len(files_changed)
        insertions = sum(1 for l in lines if l.startswith('+') and not l.startswith('+++'))
        deletions = sum(1 for l in lines if l.startswith('-') and not l.startswith('---'))
        
        summary_parts = []
        if commit:
            summary_parts.append(f"Commit: {commit}")
        elif ref_a and ref_b:
            summary_parts.append(f"Range: {ref_a}..{ref_b}")
        summary_parts.append(f"Files changed: {file_count}")
        summary_parts.append(f"Insertions: {insertions}, Deletions: {deletions}")
        
        return f"{' | '.join(summary_parts)}\n{'='*60}\n{output}"
        
    except subprocess.TimeoutExpired:
        return "Error: git diff timed out after 15 seconds."
    except Exception as e:
        return f"Error running git diff: {e}"

def register_git_diff_tool(registry, project_state: Optional[ProjectContextState] = None):
    def _tool_git_diff(repo_path: str = None, commit: str = None, ref_a: str = None,
                        ref_b: str = None, max_lines: int = 300) -> str:
        return git_diff(repo_path, commit=commit, ref_a=ref_a, ref_b=ref_b,
                         max_lines=max_lines, project_state=project_state)

    registry.register(
        name="git_diff",
        fn=_tool_git_diff,
        description="Show a commit's diff or a two-ref diff in an allowlisted local Git repo. Omit repo_path for active Project root; Project binding grants no Git authority.",
        parameters={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Allowlisted repo only: ~/lumina or ~/lumina-release. Omit for active Project root; Project binding grants no Git authority."
                },
                "commit": {
                    "type": "string",
                    "description": "Single-commit diff (hash/ref); not with ref_a/ref_b."
                },
                "ref_a": {
                    "type": "string",
                    "description": "Range start; requires ref_b; not with commit."
                },
                "ref_b": {
                    "type": "string",
                    "description": "Range end; requires ref_a; not with commit."
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Diff line cap; default 300, maximum 2000."
                }
            },
            "required": []
        }
    )
