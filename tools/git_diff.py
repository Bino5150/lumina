"""Tool: git_diff - show diffs from a commit or between refs in a local repo."""
import subprocess
import os

# Repo path allowlist — restricts this tool to known project roots. Update in
# all four git_*.py files together if a new repo needs to be added. Oracle
# paths intentionally excluded pending confirmation from Bino.
_ALLOWED_REPOS = {
    os.path.realpath(os.path.expanduser(p)) for p in ("~/lumina", "~/lumina-release")
}


def _resolve_allowed_repo(repo_path: str):
    """Returns (real_path, error_message_or_None)."""
    real = os.path.realpath(os.path.expanduser(repo_path))
    if real not in _ALLOWED_REPOS:
        allowed = ", ".join(sorted(_ALLOWED_REPOS))
        return real, f"Error: repo_path not in allowlist. Allowed: {allowed}. Got: {repo_path}"
    return real, None


def git_diff(repo_path: str, commit: str = None, ref_a: str = None, ref_b: str = None, max_lines: int = 300) -> str:
    """
    Show diff for a specific commit or between two refs in a local git repo.

    Args:
        repo_path: Absolute or ~-expanded path to a git repository.
        commit: Show the diff introduced by this commit (single commit hash/ref).
        ref_a: Start of range (branch/commit/tag). Requires ref_b.
        ref_b: End of range (branch/commit/tag). Requires ref_a.
        max_lines: Maximum diff lines to return (default 300, max 2000).

    Returns:
        Unified diff output or error message.
    """
    repo_path, _err = _resolve_allowed_repo(repo_path)
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

def register_git_diff_tool(registry):
    registry.register(
        name="git_diff",
        fn=git_diff,
        description="Show the diff of changes in a specific commit, or between two refs (branches, tags, SHAs) in a local git repository.",
        parameters={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to local git repo. Must be one of the allowlisted project roots: ~/lumina, ~/lumina-release."
                },
                "commit": {
                    "type": "string",
                    "description": "Show the diff introduced by this single commit (hash/ref). Mutually exclusive with ref_a/ref_b."
                },
                "ref_a": {
                    "type": "string",
                    "description": "Start of range. Requires ref_b. Mutually exclusive with commit."
                },
                "ref_b": {
                    "type": "string",
                    "description": "End of range. Requires ref_a. Mutually exclusive with commit."
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum diff lines to return (default 300, max 2000)."
                }
            },
            "required": ["repo_path"]
        }
    )
