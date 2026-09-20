"""
Markdown/diff -> HTML rendering for chat bubbles. Split out of ui/chat_widget.py
so this pure-string logic can be unit-tested without a PySide6 dependency
(see the CI break this fixes: tests/test_diff_bubble_coloring.py was importing
ui.chat_widget, which pulls in PySide6 at module level, and CI's test job
deliberately never installs it).
"""

import os
import re
import urllib.parse

# MEDIA-GENERATION-CONVERSATIONAL-RUNTIME-01 — the chat surface had no
# outbound image primitive at all: md_to_html() handled headers/bold/
# italic/tables/`[text](link)` but never `![alt](path)`, so a generated
# image's local path had no way to actually render in a response bubble
# (QTextBrowser natively renders a local `<img src="file://...">`, so this
# is the smallest correct addition rather than a new widget or IPC path).
# Deliberately file:// ONLY — never http(s) — so text a model might echo
# from an untrusted source (a fetched web page, provider-supplied text)
# can never turn into a live network image request from this renderer;
# see core/image_generation_service.py's "remote URL is never durable"
# law, mirrored here as "remote URL is never rendered." A path that
# doesn't resolve to a real local file is left completely untouched
# (falls through to the existing link-rendering behavior below) rather
# than ever claiming an image exists when it doesn't.
_LOCAL_IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(file://([^)\s]+)\)')

# CASTLE-WALLS-REPAIR-01 R3 -- os.path.isfile() alone (the only prior
# check) follows symlinks and trusts any local path that merely exists,
# with no containment, no identity, and no content verification. imghdr
# is gone from the stdlib on this repo's Python 3.13 runtime and Pillow
# isn't a dependency, so this is a small hand-rolled magic-byte sniff --
# real signature bytes, never trusted by extension.
_IMAGE_MAGIC_SIGNATURES = (
    (0, b"\x89PNG\r\n\x1a\n"),  # PNG
    (0, b"\xff\xd8\xff"),       # JPEG
    (0, b"GIF87a"),
    (0, b"GIF89a"),
)


def _looks_like_image_bytes(data: bytes) -> bool:
    header = data[:16]
    for offset, magic in _IMAGE_MAGIC_SIGNATURES:
        if header[offset:offset + len(magic)] == magic:
            return True
    return header[0:4] == b"RIFF" and header[8:12] == b"WEBP"  # WEBP


def _is_verified_generated_artifact(path: str) -> str | None:
    """CASTLE-WALLS-REPAIR-01 R3 -- a local path renders as a generated
    image only when it satisfies the actual Lumina artifact contract, not
    merely "some file that happens to live under the right directory":

      1. containment  -- realpath-resolved (symlinks followed to their
         real destination) and contained under the real artifact storage
         root, using the same os.path.commonpath() idiom already used
         repo-wide (worktree_manager.py, agent_backup.py, skill_transport.py);
      2. identity      -- the exact resolved path matches a genuinely-
         ingested generation_artifacts row's own recorded local_path, not
         merely a same-shaped dropped file or a symlink placed at the
         right spot;
      3. content       -- sniffs as a real, supported image format via
         magic bytes, never trusted by extension alone.

    Returns the verified, resolved path to render, or None. Fails CLOSED
    on any exception -- an error here means "not a verified artifact,"
    never "assume it's fine." Remote http(s) rendering is untouched by
    this function entirely; _LOCAL_IMAGE_RE only ever matches file://."""
    try:
        # A genuine artifact file is written once by core.generation_
        # artifact._write_atomic() (temp file + os.replace()) and is never
        # a symlink. Rejecting a symlinked reference here, BEFORE any
        # realpath resolution, closes a gap plain realpath-equality can't:
        # if the file AT a previously-legitimate recorded path is later
        # replaced by a symlink pointing elsewhere, comparing
        # realpath(referenced_path) to realpath(recorded_local_path) finds
        # them "equal" (both resolve through the SAME substituted symlink)
        # even though the underlying bytes are no longer what was ingested.
        if os.path.islink(path):
            return None
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            return None

        from core.generation_artifact import (
            ArtifactNotFound,
            _artifact_storage_root,
            get_artifact_bytes,
            get_generation_artifact,
        )
        root = os.path.realpath(_artifact_storage_root())
        if os.path.commonpath([root, real]) != root:
            return None

        artifact_id = os.path.basename(real)
        try:
            record = get_generation_artifact(artifact_id)
        except ArtifactNotFound:
            return None
        if os.path.islink(record.local_path):
            return None
        if os.path.realpath(record.local_path) != real:
            return None

        # CANNON-09: the canonical read re-hashes the on-disk bytes against
        # the immutable DB record. Path identity plus magic bytes alone let
        # an in-place regular-file replacement render as a genuine artifact.
        verified_bytes = get_artifact_bytes(artifact_id)
        return real if _looks_like_image_bytes(verified_bytes) else None
    except Exception:
        return None


def _convert_local_images(text: str) -> str:
    def _replace(match: re.Match) -> str:
        alt, raw_path = match.group(1), match.group(2)
        path = urllib.parse.unquote(raw_path)
        verified_path = _is_verified_generated_artifact(path)
        if verified_path is None:
            return match.group(0)
        safe_alt = (
            alt.replace('&', '&amp;').replace('<', '&lt;')
               .replace('>', '&gt;').replace('"', '&quot;')
        )
        src = 'file://' + urllib.parse.quote(verified_path)
        return (
            f'<img src="{src}" alt="{safe_alt}" '
            'style="max-width:100%;border-radius:6px;margin:6px 0;display:block;">'
        )
    return _LOCAL_IMAGE_RE.sub(_replace, text)


def _diff_to_html(code: str) -> str:
    """Line-by-line coloring for ```diff fenced blocks. `code` arrives already
    HTML-escaped by the caller. +++/--- file headers stay neutral (metadata,
    not a change); @@ hunk headers get accent blue; +/- lines get green/red;
    everything else (context lines) keeps the default text color."""
    lines = code.split('\n')
    rendered = []
    for line in lines:
        if line.startswith('+++') or line.startswith('---'):
            color = '#8b949e'
        elif line.startswith('+'):
            color = '#3fb950'
        elif line.startswith('-'):
            color = '#f85149'
        elif line.startswith('@@'):
            color = '#79c0ff'
        else:
            color = '#c9d1d9'
        rendered.append(f'<span style="color:{color};">{line}</span>')
    body = '<br>'.join(rendered)
    return f'<pre style="background:#0d1117;padding:10px;border-radius:4px;font-family:monospace;font-size:12px;margin:6px 0;">{body}</pre>'


def md_to_html(text: str, colors: dict) -> str:
    parts = re.split(r'(```[\s\S]*?```|`[^`]+`)', text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            if part.startswith('```'):
                lang_match = re.match(r'^```(\w*)', part)
                lang = lang_match.group(1) if lang_match else ''
                code = re.sub(r'^```\w*\n?', '', part)
                code = re.sub(r'```$', '', code).strip()
                code = code.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
                if lang == 'diff':
                    result.append(_diff_to_html(code))
                else:
                    result.append(f'<pre style="background:#0d1117;padding:10px;border-radius:4px;font-family:monospace;font-size:12px;margin:6px 0;">{code}</pre>')
            else:
                code = part.strip('`').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
                result.append(f'<code style="background:#0d1117;padding:2px 5px;border-radius:3px;font-family:monospace;font-size:12px;">{code}</code>')
        else:
            p = part
            # Must run before the `[text](url)` link regex below — that
            # regex doesn't care about a leading `!`, so it would otherwise
            # consume `![alt](file://...)` first and leave a stray `!` plus
            # a clickable link instead of an image.
            p = _convert_local_images(p)
            p = re.sub(r'^### (.+)$', rf'<h4 style="color:{colors["accent"]};margin:8px 0 4px;">\1</h4>', p, flags=re.MULTILINE)
            p = re.sub(r'^## (.+)$',  rf'<h3 style="color:{colors["accent"]};margin:10px 0 4px;">\1</h3>', p, flags=re.MULTILINE)
            p = re.sub(r'^# (.+)$',   rf'<h2 style="color:{colors["accent"]};margin:12px 0 4px;">\1</h2>', p, flags=re.MULTILINE)
            p = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', p)
            p = re.sub(r'\*(.+?)\*', r'<i>\1</i>', p)
            p = _convert_tables(p, colors)
            p = re.sub(r'^\s*[-*] (.+)$', r'<li>\1</li>', p, flags=re.MULTILINE)
            p = re.sub(r'(<li>.*?</li>)', r'<ul style="margin:4px 0;padding-left:20px;">\1</ul>', p, flags=re.DOTALL)
            p = re.sub(r'^\s*\d+\. (.+)$', r'<li>\1</li>', p, flags=re.MULTILINE)
            p = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', rf'<a href="\2" style="color:{colors["accent"]};">\1</a>', p)
            p = p.replace('\n', '<br>')
            result.append(p)
    return ''.join(result)


def _convert_tables(text: str, colors: dict) -> str:
    lines = text.split('\n')
    output, i = [], 0
    while i < len(lines):
        if '|' in lines[i] and i+1 < len(lines) and re.match(r'^\s*\|[-| :]+\|\s*$', lines[i+1]):
            headers = [c.strip() for c in lines[i].strip().strip('|').split('|')]
            i += 2
            rows = []
            while i < len(lines) and '|' in lines[i]:
                rows.append([c.strip() for c in lines[i].strip().strip('|').split('|')])
                i += 1
            th = ''.join(f'<th style="padding:6px 12px;border-bottom:1px solid #1e2133;color:{colors["accent"]};text-align:left;">{h}</th>' for h in headers)
            trs = ''.join('<tr>'+''.join(f'<td style="padding:5px 12px;border-bottom:1px solid #1e2133;">{c}</td>' for c in row)+'</tr>' for row in rows)
            output.append(f'<table style="border-collapse:collapse;margin:8px 0;width:100%;"><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>')
        else:
            output.append(lines[i]); i += 1
    return '\n'.join(output)
