"""Tool: git_log - show recent commit history in a local git repo."""
import subprocess
import os
from typing import Optional

from core.git_repos import resolve_git_repo
from core.project_context import ProjectContextState


def git_log(repo_path: Optional[str] = None, max_count: int = 20, branch: str = None, author: str = None,
            *, project_state: Optional[ProjectContextState] = None) -> str:
    """
    Show recent git commit history.

    Args:
        repo_path: Absolute or ~-expanded path to a git repository. If omitted, defaults to
            the active Project's root (still subject to the Git allowlist).
        max_count: Number of commits to show (default 20, max 100).
        branch: Optional branch name to filter by (default: current branch).
        author: Optional author pattern to filter by.
        project_state: internal — the calling agent's ProjectContextState, injected by the registrar.

    Returns:
        Formatted commit log or error message.
    """
    repo_path, _err = resolve_git_repo(repo_path, project_state)
    if _err:
        return _err

    git_dir = os.path.join(repo_path, '.git')
    if not os.path.isdir(git_dir):
        return f"Error: not a git repository: {repo_path}"

    cmd = ['git', 'log', f'--max-count={min(max_count, 100)}',
           '--format=%h %an %ar%n%s%n%b%n---']

    # --author's value is consumed as this flag's argument, never re-parsed
    # as a flag itself, so it needs no extra guarding. `branch` below is a
    # bare positional revision, so it goes after --end-of-options — placed
    # last so it doesn't swallow --author into "everything is non-option now".
    if author:
        cmd.extend(['--author', author])
    if branch:
        # --end-of-options guards against a branch value like "--output=/path"
        # being parsed as a git flag instead of a revision (argument-injection
        # hardening).
        cmd.extend(['--end-of-options', branch])
    
    try:
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return f"Git error:\n{result.stderr.strip()}"
        output = result.stdout.strip()
        if not output:
            return "No commits found matching the given criteria."
        
        # Parse into structured format
        lines = output.split('\n')
        formatted = []
        current = []
        for line in lines:
            if line == '---':
                if current:
                    entry = current[0].split(' ', 2)
                    if len(entry) >= 3:
                        hash_, author, date = entry[0], entry[1], entry[2]
                    else:
                        hash_, author, date = entry[0], '', ''
                    subject = current[1] if len(current) > 1 else ''
                    body_lines = current[2:] if len(current) > 2 else []
                    formatted.append(f"  {hash_}  {author}  {date}")
                    formatted.append(f"      {subject}")
                    for bl in body_lines:
                        bl = bl.strip()
                        if bl:
                            formatted.append(f"      {bl}")
                    formatted.append("")
                    current = []
            else:
                current.append(line)
        
        header = f"Repository: {repo_path}\n"
        if branch:
            header += f"Branch: {branch}\n"
        header += f"Commits: {len(formatted) // 3}\n\n"
        return header + '\n'.join(formatted)
        
    except subprocess.TimeoutExpired:
        return "Error: git log timed out after 15 seconds."
    except Exception as e:
        return f"Error running git log: {e}"

def register_git_log_tool(registry, project_state: Optional[ProjectContextState] = None):
    def _tool_git_log(repo_path: str = None, max_count: int = 20, branch: str = None, author: str = None) -> str:
        return git_log(repo_path, max_count=max_count, branch=branch, author=author, project_state=project_state)

    registry.register(
        name="git_log",
        fn=_tool_git_log,
        description="Read local Git history: hash, author, date and message. Omit repo_path for active Project root; allowlist still applies and Project binding grants no Git authority.",
        parameters={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Allowlisted repo only: ~/lumina or ~/lumina-release. Omit for active Project root; Project binding grants no Git authority."
                },
                "max_count": {
                    "type": "integer",
                    "description": "Commit count; default 20, maximum 100."
                },
                "branch": {
                    "type": "string",
                    "description": "Optional branch filter."
                },
                "author": {
                    "type": "string",
                    "description": "Optional author pattern."
                }
            },
            "required": []
        }
    )
