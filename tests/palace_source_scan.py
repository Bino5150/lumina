"""Shared AST source scan for the PALACE-GUARD-01B-3/4 structural guards.

"Product code" = every .py file git considers part of the working tree
(tracked, or untracked-but-not-ignored) that is not under tests/. Untracked
files must count: the authority code a campaign adds is untracked until its
checkpoint, and a guard that cannot see it would pass on exactly the tree it
exists to judge. Gitignored files (a dev checkout's reports/ scripts, its
info/exclude entries) stay out, which is what 01B-2's tracked-only helper was
protecting.
"""
import ast
import functools
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent


def product_py(root=REPO):
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard",
         "--", "*.py"],
        capture_output=True, check=True).stdout.decode("utf-8")
    return sorted({root / p for p in out.split("\0")
                   if p and not p.startswith("tests/") and (root / p).is_file()})


@functools.lru_cache(maxsize=1)
def parsed_product():
    return tuple(
        (path.relative_to(REPO).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        for path in product_py()
    )


class Scope(ast.NodeVisitor):
    """Collects (relpath, enclosing function, node) for nodes of interest.
    Bare string statements (docstrings, comments-as-strings) are not code."""

    def __init__(self, rel, want):
        self.rel, self.want, self.stack, self.hits = rel, want, [], []

    def visit_Expr(self, node):
        if isinstance(node.value, ast.Constant):
            return
        self.generic_visit(node)

    def _fn(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _fn

    def generic_visit(self, node):
        if self.want(node):
            self.hits.append((self.rel, self.stack[-1] if self.stack else "<module>", node))
        super().generic_visit(node)


def scan(want):
    hits = []
    for rel, tree in parsed_product():
        v = Scope(rel, want)
        v.visit(tree)
        hits += v.hits
    return hits


def where(want):
    """{(relpath, function)} for every hit."""
    return {(rel, fn) for rel, fn, _ in scan(want)}
