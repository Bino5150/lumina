"""Tool: git_branches - list branches in a local git repo."""
import subprocess
import os
from typing import Optional

from core.git_repos import resolve_git_repo
from core.project_context import ProjectContextState


def git_branches(repo_path: Optional[str] = None, remote: bool = False, filter: str = None,
                  *, project_state: Optional[ProjectContextState] = None) -> str:
    """
    List branches in a local git repository.

    Args:
        repo_path: Absolute or ~-expanded path to a git repository. If omitted, defaults to
            the active Project's root (still subject to the Git allowlist).
        remote: If True, also show remote-tracking branches.
        filter: Optional substring to filter branches by.
        project_state: internal — the calling agent's ProjectContextState, injected by the registrar.

    Returns:
        Formatted list of branches with current branch marked.
    """
    repo_path, _err = resolve_git_repo(repo_path, project_state)
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

def register_git_branches_tool(registry, project_state: Optional[ProjectContextState] = None):
    def _tool_git_branches(repo_path: str = None, remote: bool = False, filter: str = None) -> str:
        return git_branches(repo_path, remote=remote, filter=filter, project_state=project_state)

    registry.register(
        name="git_branches",
        fn=_tool_git_branches,
        description="List all local and remote branches in a git repository, showing which is currently active. "
                     "If repo_path is omitted, defaults to the active Project's root — which must still be an "
                     "allowlisted Git repository (a Project binding alone does not authorize a repo for Git tools).",
        parameters={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to local git repo. Must be one of the allowlisted project roots: "
                                    "~/lumina, ~/lumina-release. If omitted, defaults to the active Project's root "
                                    "(still subject to the same allowlist)."
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
            "required": []
        }
    )
