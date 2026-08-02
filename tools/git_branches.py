"""Tool: git_branches - list branches in a local git repo."""
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


def git_branches(repo_path: str, remote: bool = False, filter: str = None) -> str:
    """
    List branches in a local git repository.

    Args:
        repo_path: Absolute or ~-expanded path to a git repository.
        remote: If True, also show remote-tracking branches.
        filter: Optional substring to filter branches by.

    Returns:
        Formatted list of branches with current branch marked.
    """
    repo_path, _err = _resolve_allowed_repo(repo_path)
    if _err:
        return _err
    if not os.path.isdir(repo_path):
        return f"Error: path not found: {repo_path}"
    if not os.path.isdir(os.path.join(repo_path, '.git')):
        return f"Error: not a git repository: {repo_path}"
    
    parts = []
    
    # Local branches
    cmd = ['git', 'branch']
    if remote:
        cmd.append('-a')
    if filter:
        cmd.extend(['--list', f'*{filter}*'])
    
    try:
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return f"Git error:\n{result.stderr.strip()}"
        
        output = result.stdout.strip()
        if not output:
            return "No branches found matching the given criteria."
        
        lines = output.split('\n')
        local_count = sum(1 for l in lines if not l.strip().startswith('remotes/'))
        remote_count = sum(1 for l in lines if l.strip().startswith('remotes/'))
        
        header = f"Repository: {repo_path}\n"
        header += f"Branches: {len(lines)} total"
        if remote:
            header += f" ({local_count} local, {remote_count} remote)"
        header += "\n\n"
        
        # Format nicely
        formatted = []
        for line in lines:
            line = line.strip()
            if line.startswith('* '):
                formatted.append(f"  ▶ {line[2:]}  (current)")
            elif line.startswith('remotes/'):
                formatted.append(f"     {line}")
            else:
                formatted.append(f"    {line}")
        
        return header + '\n'.join(formatted)
        
    except subprocess.TimeoutExpired:
        return "Error: git branches timed out."
    except Exception as e:
        return f"Error: {e}"

def register_git_branches_tool(registry):
    registry.register(
        name="git_branches",
        fn=git_branches,
        description="List all local and remote branches in a git repository, showing which is currently active.",
        parameters={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to local git repo. Must be one of the allowlisted project roots: ~/lumina, ~/lumina-release."
                },
                "remote": {
                    "type": "boolean",
                    "description": "If True, also show remote-tracking branches (default False)."
                },
                "filter": {
                    "type": "string",
                    "description": "Optional substring to filter branches by."
                }
            },
            "required": ["repo_path"]
        }
    )
