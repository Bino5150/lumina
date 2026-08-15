"""
Vision Tools — path-based image awareness (NOT image loading).

2026-08-15: added after a live incident where Bino asked Lumina (Gemini
backend) to look at an image by file path. She had no tool for this, so she
improvised -- list_tools(), then run_python() poking at os.environ, list_dir(),
even opening config.py -- burning six separate round-trips to Gemini before
ever producing an answer. Gemini's free tier caps generate_content_free_tier_
requests at 5, so the continuation after that flailing got a 429 before she
could say anything useful.

Root cause is a real protocol limit, not a missing feature to bolt on
casually: Gemini's functionResponse (see gemini_backend.py's docstring) wants
a plain JSON object, not inline image data -- there is no wire-level way for
a tool RESULT to hand the model actual pixel data. Making path-based vision
genuinely work would mean core/agent.py's chat() loop synthesizing a real
follow-up multipart *user* turn after the tool call, which is materially
bigger and riskier than this fix -- deliberately out of scope here.

This tool's job is narrower and safe: confirm the path is a real image, then
say so in ONE call, so the model stops improvising and tells the user to use
the chat UI's drag-and-drop instead (the only path that actually works --
see main_window.py's _on_files_dropped(), which builds the image directly
into the request before it ever reaches the model).
"""

import os

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}


def register_vision_tools(registry):

    def view_image(path: str) -> str:
        """Check whether a path points to a real image file. Does NOT load
        or view its contents -- no tool can hand image pixel data back into
        this conversation. Use this to confirm a path is valid, then tell
        the user to attach/drag the file into the chat UI instead, which is
        the only way to actually see an image."""
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return f"[Error: file not found: {path}]"
        if not os.path.isfile(path):
            return f"[Error: not a file: {path}]"
        ext = os.path.splitext(path)[1].lower()
        if ext not in IMAGE_EXTS:
            return f"[{path} exists but its extension ({ext or 'none'}) doesn't look like a supported image format.]"
        size = os.path.getsize(path)
        return (
            f"[{path} is a real {size}-byte {ext} image file, but there is no tool that can load "
            "its pixel data into this conversation -- vision only works through the chat UI's "
            "attach/drag-and-drop, which builds the image directly into the request before it "
            "reaches me. Tell the user to drop or attach that file in the chat window instead of "
            "giving a path; do not attempt to read, open, or otherwise access the file's contents "
            "via other tools -- none of them can make it visible to you either.]"
        )

    registry.register(
        name="view_image",
        fn=view_image,
        description=(
            "Check whether a file path points to a real, valid image. IMPORTANT: this does NOT "
            "let you see the image's contents -- no tool call can load pixel data into your "
            "context, on any backend. If the user references an image by file path, call this "
            "once to confirm it exists, then ask them to attach/drag it into the chat UI instead "
            "-- that is the only way you can actually view an image. Do not try read_file, "
            "run_python, or any other tool to work around this; none of them can succeed either."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to check. Supports ~ expansion."}
            },
            "required": ["path"]
        }
    )
