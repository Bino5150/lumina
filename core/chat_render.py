"""
Markdown/diff -> HTML rendering for chat bubbles. Split out of ui/chat_widget.py
so this pure-string logic can be unit-tested without a PySide6 dependency
(see the CI break this fixes: tests/test_diff_bubble_coloring.py was importing
ui.chat_widget, which pulls in PySide6 at module level, and CI's test job
deliberately never installs it).
"""

import re


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
