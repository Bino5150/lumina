"""Tool: git_log - show recent commit history in a local git repo."""
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


def git_log(repo_path: str, max_count: int = 20, branch: str = None, author: str = None) -> str:
    """
    Show recent git commit history.

    Args:
        repo_path: Absolute or ~-expanded path to a git repository.
        max_count: Number of commits to show (default 20, max 100).
        branch: Optional branch name to filter by (default: current branch).
        author: Optional author pattern to filter by.

    Returns:
        Formatted commit log or error message.
    """
    repo_path, _err = _resolve_allowed_repo(repo_path)
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

def register_git_log_tool(registry):
    registry.register(
        name="git_log",
        fn=git_log,
        description="Show recent git commit history for a local repository. Returns a formatted log with hash, author, date, and message.",
        parameters={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to local git repo. Must be one of the allowlisted project roots: ~/lumina, ~/lumina-release."
                },
                "max_count": {
                    "type": "integer",
                    "description": "Number of commits to show (default 20, max 100)."
                },
                "branch": {
                    "type": "string",
                    "description": "Optional branch name to filter by."
                },
                "author": {
                    "type": "string",
                    "description": "Optional author pattern to filter by."
                }
            },
            "required": ["repo_path"]
        }
    )
