"""
Lumina Agent Loop
Full turn cycle: receive → think → tool calls → stream final response.
"""

import re
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core.backends.loader import get_llm_backend
from core.context import ContextManager
from tools.registry import ToolRegistry
from tools.meta import register_meta_tools
from tools.memory import register_memory_tools, init_memory_db
from tools.knowledge import register_knowledge_tools
from tools.web import register_web_tools
from tools.filesystem import register_filesystem_tools
from tools.sandbox import register_sandbox_tools
from tools.terminal import register_terminal_tools
from tools.toolmaker import register_toolmaker_tools
from tools.palace import register_palace_tools
from core.skills import register_skills_tools, build_skills_block, init_skills_db
from core.chat_history import register_chat_history_tools
from tools.projects import register_projects_tools, init_projects
from tools.diff import register_diff_tools
from tools.browser import register_browser_tools, browser_manager


CHAIN_BLOCKED_AFTER_SEARCH = {"get_website", "web_search"}


def strip_think_blocks(text: str) -> str:
    if not text:
        return text
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()


class LuminaAgent:
    def __init__(self,
                 on_tool_call=None,
                 on_tool_result=None,
                 on_think_start=None,
                 on_think_token=None,
                 on_think_end=None,
                 on_response_token=None,
                 tts=None):
        """
        Streaming callbacks:
          on_tool_call(name, args)     — tool about to execute
          on_tool_result(name, result) — tool finished
          on_think_start(step)         — <think> block opened
          on_think_token(token)        — character inside think block
          on_think_end()               — </think> block closed
          on_response_token(token)     — final response token streaming
        """
        self.llm = get_llm_backend()
        self.ctx = ContextManager()
        self.registry = ToolRegistry()

        self.on_tool_call     = on_tool_call     or (lambda n, a: None)
        self.on_tool_result   = on_tool_result   or (lambda n, r: None)
        self.on_think_start   = on_think_start   or (lambda step: None)
        self.on_think_token   = on_think_token   or (lambda t: None)
        self.on_think_end     = on_think_end     or (lambda: None)
        self.on_response_token = on_response_token or (lambda t: None)
        self.tts = tts
        self.persona_avatar = None  # set by apply_persona()
        self._session_tool_calls = 0       # total tool calls this session
        self._skill_nudge_sent   = False   # only nudge once per session

        init_memory_db()
        register_meta_tools(self.registry, self.ctx)
        register_memory_tools(self.registry)
        register_knowledge_tools(self.registry)
        register_web_tools(self.registry)
        register_filesystem_tools(self.registry)
        register_sandbox_tools(self.registry)
        register_terminal_tools(self.registry)
        register_toolmaker_tools(self.registry, self)
        register_palace_tools(self.registry)
        from core.persistence import load as load_prefs
        _bio = load_prefs().get("human_bio", "").strip()
        if _bio:
            self.ctx.system_prompt += f"\n\n## About {config.USER_NAME}\n{_bio}"
        _disabled_tools = load_prefs().get("disabled_tools", [])
        if _disabled_tools:
            self.registry.set_disabled(_disabled_tools)
        init_skills_db()
        register_skills_tools(self.registry)   
        register_chat_history_tools(self.registry) 
        init_projects()
        register_projects_tools(self.registry)
        register_diff_tools(self.registry)
        register_browser_tools(self.registry)
    def chat(self, user_input: str) -> str:
        """
        Main entry point. Runs tool loop with non-streaming,
        then streams the final response. Returns full response string.
        """
        self.ctx.add_user(user_input)
        tools_used_this_turn = set()
        think_step = [0]
        
        # Inject relevant skill docs into system prompt for this turn
        skills_block = build_skills_block(user_input)
        if skills_block:
            self.ctx.push_ephemeral(skills_block)

        for iteration in range(config.MAX_TOOL_ITERATIONS):
            tool_schemas = self.registry.get_schemas()
            tool_token_estimate = self.registry.schema_token_estimate()
            messages = self.ctx.build_messages(tool_budget=tool_token_estimate)

            try:
                response = self.llm.chat(
                    messages=messages,
                    tools=tool_schemas,
                    max_tokens=config.RESPONSE_RESERVE_TOKENS,
                )
            except Exception as e:
                print(f"[AGENT ERROR] {type(e).__name__}: {e}", flush=True)
                return f"[Lumina error: {e}]"

            message = self.llm.extract_message(response)

            # No tool calls — stream the final response
            if not self.llm.is_tool_call(message):
                return self._stream_final(messages, think_step)

            # Has tool calls
            if message.get("content"):
                message["content"] = strip_think_blocks(message["content"])

            self.ctx.add_tool_call(message)
            tool_calls = self.llm.get_tool_calls(message)

            for tc in tool_calls:
                tool_id = tc.get("id", "unknown")
                name, args = self.llm.parse_tool_call(tc)

                if "web_search" in tools_used_this_turn and name in CHAIN_BLOCKED_AFTER_SEARCH:
                    result = "[Skipped: summarize from search results already provided.]"
                    self.ctx.add_tool_result(tool_id, name, result)
                    continue

                self.on_tool_call(name, args)
                try:
                    result = self.registry.call(name, args)
                except Exception as e:
                    result = f"[Tool error: {name} failed — {e}]"
                    print(f"[TOOL ERROR] {name}: {e}", flush=True)
                self.on_tool_result(name, result)
                tools_used_this_turn.add(name)
                self.ctx.add_tool_result(tool_id, name, result)
                self._session_tool_calls += 1
                
                # Nudge skill creation after threshold — once per session
            if (not self._skill_nudge_sent
                    and self._session_tool_calls >= config.SKILLS_TRIGGER_THRESHOLD):
                self._skill_nudge_sent = True
                self.ctx.add_user(
                    "That workflow involved several tool calls. "
                    "If this procedure is reusable, consider calling save_skill() "
                    "to save it for future sessions — before giving your final answer."
                )

        # Max iterations — force final streamed answer
        messages = self.ctx.build_messages()
        messages.append({"role": "user", "content": "Give your final answer now based on what you have."})
        return self._stream_final(messages, think_step)

    def _stream_final(self, messages: list, think_step: list) -> str:
        """Stream the final response, firing callbacks for UI updates."""
        full_response = []
        in_think = False

        try:
            for chunk in self.llm.chat_stream(
                messages=messages,
                max_tokens=config.RESPONSE_RESERVE_TOKENS
            ):
                print(f"[STREAM] {repr(chunk)}", flush=True)
                if chunk == "__THINK_START__":
                    in_think = True
                    think_step[0] += 1
                    self.on_think_start(think_step[0])
                elif chunk == "__THINK_END__":
                    in_think = False
                    self.on_think_end()
                elif in_think:
                    self.on_think_token(chunk)
                else:
                    self.on_response_token(chunk)
                    full_response.append(chunk)

        except (ConnectionError, TimeoutError) as e:
            err = f"[Stream error: {e}]"
            self.on_response_token(err)
            return err

        content = "".join(full_response).strip()
        self.ctx.add_assistant(content)
        if self.tts and content:
            self.tts.speak(content)
        return content
    
    def apply_persona(self, persona: dict):
        """Hot-swap agent identity from a persona dict."""
        import config

        # 1. Name
        if "name" in persona:
            config.AGENT_NAME = persona["name"]
            self.persona_avatar = persona.get("avatar")

        # 2. System prompt
        if "system_prompt" in persona:
            from core.persistence import load as load_prefs
            new_prompt = persona["system_prompt"]
            bio = load_prefs().get("human_bio", "").strip()
            if bio:
                new_prompt += f"\n\n## About {config.USER_NAME}\n{bio}"
            self.ctx.update_system_prompt(new_prompt)

        # 3. Tool set
        if "tools_enabled" in persona:
            all_tools = [s["function"]["name"] for s in self.registry.get_schemas()]
            disabled = [t for t in all_tools if t not in persona["tools_enabled"]]
            self.registry.set_disabled(disabled)

        # 4. TTS voice + settings
        if self.tts:
            if "tts_voice" in persona:
                if hasattr(self.tts, 'set_profile'):
                    self.tts.set_profile(persona["tts_voice"])
                elif hasattr(self.tts, 'set_voice'):
                    self.tts.set_voice(persona["tts_voice"])
            if "tts_speed" in persona:
                self.tts.speed = persona["tts_speed"]
            if "tts_pitch" in persona:
                self.tts.pitch = persona["tts_pitch"]
            if "tts_volume" in persona:
                self.tts.volume = persona["tts_volume"]

        print(f"[PERSONA] Applied: {persona.get('name', 'unknown')}", flush=True)

    def test_connection(self) -> str:
        ok, msg = self.llm.health_check()
        return msg

    def get_token_count(self) -> int:
        return self.ctx.token_count()
