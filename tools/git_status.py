"""Tool: git_status - show working tree state of a local git repo."""
import subprocess, os

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


def git_status(repo_path: str) -> str:
    repo_path, _err = _resolve_allowed_repo(repo_path)
    if _err:
        return _err
    if not os.path.isdir(repo_path):
        return f"Error: path not found: {repo_path}"
    if not os.path.isdir(os.path.join(repo_path, '.git')):
        return f"Error: not a git repository: {repo_path}"

    parts = [f"Repository: {repo_path}"]
    try:
        # Branch
        r = subprocess.run(['git','rev-parse','--abbrev-ref','HEAD'], cwd=repo_path, capture_output=True, text=True, timeout=5)
        branch = r.stdout.strip() if r.returncode == 0 else "(detached HEAD)"
        parts.append(f"Branch: {branch}")

        # HEAD hash
        r = subprocess.run(['git','rev-parse','--short','HEAD'], cwd=repo_path, capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            parts.append(f"HEAD: {r.stdout.strip()}")

        # Upstream + ahead/behind
        r = subprocess.run(['git','rev-parse','--abbrev-ref','--symbolic-full-name','@{upstream}'], cwd=repo_path, capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            upstream = r.stdout.strip()
            r2 = subprocess.run(['git','rev-list','--count','--left-right',f'HEAD...{upstream}'], cwd=repo_path, capture_output=True, text=True, timeout=10)
            if r2.returncode == 0 and r2.stdout.strip():
                counts = r2.stdout.strip().split('\t')
                if len(counts) == 2:
                    parts.append(f"Ahead: {counts[0]}, Behind: {counts[1]}")
        else:
            parts.append("Upstream: (none)")

        # Status short
        r = subprocess.run(['git','status','--short'], cwd=repo_path, capture_output=True, text=True, timeout=10)
        staged, unstaged, untracked = [], [], []
        if r.returncode == 0 and r.stdout.strip():
            for line in r.stdout.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                if line.startswith('??'):
                    untracked.append(line[3:])
                elif line[0] != ' ' and line[0] != '?':
                    staged.append(line)
                else:
                    unstaged.append(line)

        parts.append("")
        parts.append(f"Staged ({len(staged)}):")
        for s in staged[:30]:
            parts.append(f"  {s}")
        if len(staged) > 30:
            parts.append(f"  ... and {len(staged)-30} more")
        if not staged:
            parts.append("  (none)")

        parts.append(f"Unstaged ({len(unstaged)}):")
        for s in unstaged[:30]:
            parts.append(f"  {s}")
        if len(unstaged) > 30:
            parts.append(f"  ... and {len(unstaged)-30} more")
        if not unstaged:
            parts.append("  (none)")

        parts.append(f"Untracked ({len(untracked)}):")
        for s in untracked[:20]:
            parts.append(f"  {s}")
        if len(untracked) > 20:
            parts.append(f"  ... and {len(untracked)-20} more")
        if not untracked:
            parts.append("  (none)")

        return '\n'.join(parts)
    except subprocess.TimeoutExpired:
        return "Error: git status timed out."
    except Exception as e:
        return f"Error: {e}"

def register_git_status_tool(registry):
    registry.register(
        name="git_status",
        fn=git_status,
        description="Show current working tree state: branch, commits ahead/behind, staged/unstaged/untracked files.",
        parameters={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to local git repo. Must be one of the allowlisted project roots: ~/lumina, ~/lumina-release."
                }
            },
            "required": ["repo_path"]
        }
    )
