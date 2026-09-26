# Personas & Skills

## Personas

Personas in Lumina are first-class objects — identity, system prompt,
voice profile/TTS assignment, and tool-profile behavior all travel
together, and switching is instant (no restart, voice swaps live).

The public release currently ships three selectable personas:

- **Lumina** — the default, protected persona.
- **Mara Voss**
- **Mr. Robot**

(A fourth file, `discord_template.json`, exists but is marked
`channel_bound` — it never appears in the normal persona picker. Its
`tools_profile` field is documentation-only: Discord's actual tool access
is hardcoded in `comms/discord_bridge.py`, not read from this file — see
[Channels](channels.md).)

Third-party character personas (voices and avatars cloned from existing
media) have been removed from the public release to avoid copyright
issues. If you're interested in acquiring them for personal,
educational, or research use believed in good faith to fall under Fair
Use, contact the dev team. You're free to create your own personas for
personal use regardless — each one is just a JSON file in `personas/`,
and Import/Export makes them shareable.

Voice cloning with reference audio for a persona requires the Chatterbox
Turbo or Voicebox TTS backend — see [Multimodal](multimodal.md).

## Skills

Beyond episodic memory, Lumina has a skills layer — procedural `.md`
documents she can write, update, and retrieve herself, indexed with a
genuine FTS5 full-text index and automatically injected into the system
prompt when relevant. After roughly 5 tool calls in a session, she's
nudged to consider saving a skill; she can also self-direct skill
creation at any time.

There's a real trust split worth knowing about: **official** skills
(bundled with the app, integrity-hash-verified at load time) go through
the normal trusted system-prompt channel. **Your own** self-authored
skills go through a separate, lower-trust channel — they don't carry the
same system-prompt authority official skills do. Two official skill
packages currently ship: a generated-artifact manifest skill and a
media-generation skill.

(A skill import/export "transporter" exists in the code
(`core/skill_transport.py`) but has no UI or tool caller anywhere in the
current build — it's tested internal plumbing, not something you can
reach today.)

## Projects — Persistent Workspaces

A Project is a persistent workspace giving Lumina both long-term
knowledge about what you're building and a real execution context for
working on it: a running handoff doc (`project.md`), a searchable
codebase map (`codebase.md`), linked conversations, and a machine-local
binding to that project's actual working tree on this machine.

Projects are execution *frames*, not sandboxes — activating one doesn't
grant new filesystem or Git authority; explicit paths stay explicit.

Full current tool set: `create_project`, `load_project`,
`update_project`, `refresh_codebase_index`, `load_codebase`, `link_chat`,
`get_project_chats`, `set_project_root`, `activate_project`,
`get_active_project`, `clear_active_project` — the last four handle
binding/activating a project's working tree and aren't just aliases of
the others.
