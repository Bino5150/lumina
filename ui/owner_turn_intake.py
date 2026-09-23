"""Qt-free owner-turn intake: where dropped-file bytes are kept apart from
owner-authored text, from file drop through to the LuminaAgent.chat() call.

CASTLE-WALLS-BLOCKING-COVERAGE-01B -- extracted verbatim from
ui/main_window.py (_on_files_dropped()'s text/fallback branches,
_on_user_message()'s attachment packaging, AgentWorker.run()'s chat() call
construction) so the blocking CI gate, which deliberately never installs
PySide6, executes the exact code that decides whether a dropped file's bytes
stay FILE_CONTENT data (CASTLE-WALLS-REPAIR-01 R1A) or become indistinguishable
owner text. Behavior is unchanged. ui/main_window.py keeps only the Qt adapter
around these calls, covered by the separate Qt guard job
(.github/workflows/tests.yml).

Nothing here may import PySide6 or any ui module that does.
"""
import inspect
import os

# Typed text files are read whole; anything else that isn't an image or audio
# (extensionless, dotfiles like .env, trailing-dot or unknown extensions) takes
# the fallback read, capped at FALLBACK_READ_CHARS. Both stage the same way.
TEXT_FILE_EXTS = frozenset({
    '.txt', '.md', '.py', '.js', '.ts', '.json', '.csv',
    '.yaml', '.yml', '.toml', '.ini', '.sh', '.html',
    '.css', '.xml', '.log',
})
FALLBACK_READ_CHARS = 8192


def admit_dropped_text_file(path: str, stage) -> str | None:
    """Read one dropped non-image, non-audio file and hand its contents ONLY
    to stage(filename, contents), which stages it as a separate attachment.

    Returns a note for the owner's editable input box -- None on success,
    otherwise a marker naming the path. The note never carries the file's
    bytes, whatever the filename or extension. A failure inside stage() is
    reported through the same note as a read failure (the pre-extraction
    handler ran staging inside the same try).
    """
    if os.path.splitext(path)[1].lower() in TEXT_FILE_EXTS:
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                contents = f.read()
            stage(os.path.basename(path), contents)
        except Exception as e:
            return f"[file:{path}] (read error: {e})"
        return None

    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            contents = f.read(FALLBACK_READ_CHARS)
        stage(os.path.basename(path), contents)
    except Exception:
        return f"[file:{path}]"
    return None


def package_submission(content, pending_text_attachments):
    """Return (content, attachments_for_chat, attachment_markers) for one
    owner submission.

    content -- the owner's own turn (a string, or an image/audio content
    list) -- is returned unchanged. Staged (filename, text) pairs become
    ("FILE_CONTENT: <filename>", text) attachments for LuminaAgent.chat()'s
    attachments= argument, where core/context.py's add_user() tags each one as
    lower-trust data. attachments_for_chat is None when nothing is staged.
    """
    if not pending_text_attachments:
        return content, None, ""
    attachments_for_chat = [
        (f"FILE_CONTENT: {fname}", file_text)
        for fname, file_text in pending_text_attachments
    ]
    attachment_markers = " ".join(
        f"[📎 {fname}]" for fname, _ in pending_text_attachments
    )
    return content, attachments_for_chat, attachment_markers


def chat_accepts(agent, param: str) -> bool:
    """Whether agent.chat() takes `param` (or **kwargs). Lightweight GUI-test
    agent stubs predate cancel_event, reasoning_effort, attachments and
    approval_event_id; an unintrospectable chat() counts as accepting."""
    try:
        params = inspect.signature(agent.chat).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(p.name == param or p.kind == inspect.Parameter.VAR_KEYWORD for p in params)


def owner_chat_call(agent, user_input, *, chat_id, cancel_event, attachments,
                    approval_event_id):
    """Return (user_input, kwargs) for the worker's agent.chat() call.

    user_input is passed through unchanged; attachments travel only as the
    separate attachments= keyword. Attachments are dropped (never fused into
    user_input) for a stub chat() that cannot accept them.
    """
    kwargs = {"chat_id": chat_id}
    if chat_accepts(agent, "cancel_event"):
        kwargs["cancel_event"] = cancel_event
    # Patch 3A.4 Part 4 -- resolved fresh on every turn against the actual
    # live backend in use (agent.llm), never cached on the worker or the
    # agent, since Settings can swap the active backend between turns.
    if chat_accepts(agent, "reasoning_effort"):
        llm = getattr(agent, "llm", None)
        if llm is not None:
            from core.reasoning_preferences import resolve_reasoning_effort
            kwargs["reasoning_effort"] = resolve_reasoning_effort(llm)
    if attachments and chat_accepts(agent, "attachments"):
        kwargs["attachments"] = attachments
    # CASTLE-WALLS-REPAIR-04 / CANNON-11 -- None (every ordinary local GUI
    # turn) leaves approval_event_id out entirely so chat() mints its own.
    if approval_event_id and chat_accepts(agent, "approval_event_id"):
        kwargs["approval_event_id"] = approval_event_id
    return user_input, kwargs
