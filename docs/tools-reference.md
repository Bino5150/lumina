# Tools Reference

The full built-in tool catalog, generated from the live registry — 105
tool names in the maximal case (owner session, Subagents and Background
Tasks enabled, Browser Companion installed and paired). A default
install without those optional pieces sees roughly 90.

This list is kept in sync with the live registry by
`tests/test_docs_tool_catalog_sync.py`, which fails the build if a tool is
registered without a corresponding row here, or if a row here names a
tool that no longer exists. If you're reading this and a tool you expect
is missing, that test should have caught it — open an issue.

Availability notes: **owner-only** = structurally absent from a non-owner
session (see [Security & Authority](security.md)); **flag-gated** = off by
default, enabled via `SUBAGENTS_ENABLED`/`BACKGROUND_TASKS_ENABLED`;
**paired-only** = only registers if Browser Companion is installed and
paired for this data directory.

## Coding, Git & Review

| Tool | Purpose |
|---|---|
| `git_status` | Structured Git status of the working tree. |
| `git_diff` | Structured diff (staged/unstaged/untracked). |
| `git_log` | Commit history. |
| `git_branches` | Branch listing/state. |
| `diff_texts` | Compare two text blocks. |
| `diff_files` | Compare two files. |
| `apply_patch` | Apply a unified diff/patch. |
| `edit_file` | Targeted, surgical file edit. |
| `search_code` | Recursive source search, literal or regex. |
| `review_changes` | Structured review of repository changes. |
| `review_file_diff` | Inspect a unified diff for one file. |
| `read_coding_checkpoint` | Read a saved coding checkpoint's state. |
| `save_coding_checkpoint` | Record repository identity/state/evidence for later freshness checks. |
| `run_tests` | Run pytest through the managed execution path, capturing real pass/fail. |
| `create_worktree` | Create an isolated, verified Git worktree. |
| `list_worktrees` | List managed worktrees. |
| `remove_worktree` | Safely remove a managed worktree. |
| `run_command` | Shell access for builds, package managers, Git, and other CLI tools (gated by Tier-1/Tier-2 guardrails — see [Security & Authority](security.md)). |

## Persistent Processes

| Tool | Purpose |
|---|---|
| `start_process` | Start a long-running program. |
| `read_process` | Read a running process's output incrementally. |
| `send_process_input` | Send input to a running process. |
| `stop_process` | Stop a running process. |
| `list_processes` | List active managed processes. |

## Sandboxed Execution

| Tool | Purpose |
|---|---|
| `run_python` | Execute Python and inspect the result. |

## Filesystem

| Tool | Purpose |
|---|---|
| `read_file` | Read a file. |
| `write_file` | Write a file. |
| `list_dir` | List a directory. |
| `search_files` | Search file contents/names. |

There is no copy, move, rename, or delete-file tool in the registry.

## Web

| Tool | Purpose |
|---|---|
| `web_search` | General web search. |
| `get_website` | Lightweight HTTP fetch of a static page (requests + BeautifulSoup). |
| `get_wikipedia` | Wikipedia lookup. |

## Browser Automation (Playwright)

| Tool | Purpose |
|---|---|
| `browser_navigate` | Navigate to a URL. |
| `browser_click` | Click an element. |
| `browser_type` | Type into a field. |
| `browser_screenshot` | Capture a screenshot. |
| `browser_extract` | Extract selected page content. |
| `browser_scroll` | Scroll the page. |
| `browser_get_links` | Enumerate links. |
| `browser_current_url` | Read the current URL/title. |
| `browser_close` | Close browser resources. |

Headless by default (`LUMINA_BROWSER_HEADLESS=0` to watch). Entirely
separate from Browser Companion below — neither falls back to the other.

## Browser Companion (owner-only, paired-only)

| Tool | Purpose |
|---|---|
| `chrome_status` | Companion connection/pairing status. |
| `chrome_list_tabs` | List tabs in your own paired Chrome profile. |
| `chrome_get_active_tab` | Read the active tab. |
| `chrome_get_url_title` | Read one tab's URL/title. |
| `chrome_extract_visible_text` | Read bounded visible text from an allowed site. |
| `chrome_get_links` | Enumerate links on an allowed site. |
| `chrome_open_owner_url` | Open a URL *you* typed this turn — see [Browser Companion](browser-companion.md). |
| `chrome_switch_tab` | Switch to an already-open tab by exact identity. |
| `chrome_follow_link` | Follow one link delivered by `chrome_get_links` on that exact document and verified again before navigation — see [Browser Companion](browser-companion.md). |

## Memory

| Tool | Purpose |
|---|---|
| `save_memory` | Save a flat memory entry. |
| `search_memory` | Search flat memory. |
| `get_recent_memories` | List recent flat memories. |
| `delete_memory` | Delete a memory and the MemPalace copies saved with it (staged — Tier-2 approval gate). |

## MemPalace

| Tool | Purpose |
|---|---|
| `palace_remember` | Write into the Wings/Rooms/Closets/Drawers hierarchy. Layers 0/1 are owner-granted only: asking for one stores at Layer 2 and stages a promotion request for you to review in Pending Actions. |
| `palace_hall` | Store a cross-cutting fact in a Hall (facts/events/preferences/advice/discoveries). Layers 0/1 follow the same rule as `palace_remember`. |
| `palace_recall` | Keyword search over the MemPalace. |
| `palace_status` | MemPalace state/summary. |
| `palace_review_writes` | List flagged/reviewable writes. |
| `palace_undo_write` | Undo a single dream or compaction write in the nightstand (provenance-stamped by this version); refuses anything else. |

## Knowledge Base

| Tool | Purpose |
|---|---|
| `save_knowledge` | Save a reference entry. |
| `search_knowledge` | Keyword-search the knowledge base. |
| `list_knowledge` | List entries by category. |
| `read_knowledge` | Read one entry in full. |
| `delete_knowledge` | Delete an entry (staged — Tier-2 approval gate). |
| `save_person` | Save a person's info to the people directory. |
| `search_people` | Search the people directory. |

## Chat History

| Tool | Purpose |
|---|---|
| `search_chat_history` | Full-text (FTS5) search over the raw message log. |
| `get_chat_session` | Fetch one chat session. |
| `list_recent_chats` | List recent chat sessions. |

## Skills

| Tool | Purpose |
|---|---|
| `save_skill` | Write a procedural skill document. |
| `list_skills` | List known skills. |
| `recall_skill` | Retrieve a skill by name/topic. |

## Projects

| Tool | Purpose |
|---|---|
| `create_project` | Create a new Project. |
| `load_project` | Load a Project's context. |
| `update_project` | Update `project.md`. |
| `refresh_codebase_index` | Rebuild a Project's `codebase.md` map. |
| `load_codebase` | Load a Project's codebase map. |
| `link_chat` | Associate a chat with a Project. |
| `get_project_chats` | List chats linked to a Project. |
| `set_project_root` | Bind a Project's execution root on this machine. |
| `activate_project` | Make a Project the active execution frame. |
| `get_active_project` | Read the currently active Project. |
| `clear_active_project` | Clear the active Project. |

## Multimodal

| Tool | Purpose |
|---|---|
| `view_image` | Confirm a path is a valid image — never loads pixel data (see [Multimodal](multimodal.md)). |
| `estimate_image_generation` | Stage an image-generation draft with a cost estimate (owner-only). |
| `generate_image` | Consume a staged draft and generate the image (owner-only). |

## Meta & Runtime Introspection

| Tool | Purpose |
|---|---|
| `get_time` | Current date/time. |
| `list_tools` | List the currently available tools. |
| `view_prompt` | View the current system prompt. |
| `edit_prompt` | Edit the system prompt (staged — Tier-2 approval gate). |
| `reset_chat` | Reset the current chat (staged — Tier-2 approval gate). |

## Toolmaker (owner-only)

| Tool | Purpose |
|---|---|
| `create_tool` | Generate a new custom tool implementation. |
| `list_pending_tools` | List tools awaiting approval. |
| `show_pending_tool_source` | Show a pending tool's source before approving it. |
| `reject_pending_tool` | Reject a staged tool. |
| `list_custom_tools` | List approved custom tools. |
| `delete_tool` | Delete a custom tool. |

## Subagents & Background/Scheduled Tasks (flag-gated, off by default)

| Tool | Purpose |
|---|---|
| `spawn_subagent` | Delegate a task to a child agent with its own isolated context and explicit tool surface. |
| `run_background_subagent` | Run delegated work in the background. |
| `schedule_background_subagent` | Schedule delegated work to run later. |
| `check_background_task` | Check a background/scheduled task's result. |

## PIN & Telegram

| Tool | Purpose |
|---|---|
| `submit_pin` | Submit a PIN to unlock sensitive-tier tools for this session. |
| `send_telegram_message` | Send a message via the Telegram bridge. |
| `send_telegram_file` | Send a file via the Telegram bridge. |

## Other

| Tool | Purpose |
|---|---|
| `check_for_updates` | Check for a newer release. |
| `get_weather` | Example custom tool — only reachable after being approved through the Toolmaker pipeline; not statically registered. |
