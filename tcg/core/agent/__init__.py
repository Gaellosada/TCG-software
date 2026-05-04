"""Agent module -- Anthropic-powered MongoDB backtester assistant.

Public API:
- ``AgentSession``   — one multi-turn conversation with tool-use loop
- ``AgentWorkspace`` — on-disk session storage (conversations, assumptions)
- ``create_tools``   — factory for tool definitions + executors
- ``build_system_prompt`` — full backtester system prompt
"""

from tcg.core.agent.prompt import build_system_prompt
from tcg.core.agent.session import AgentSession
from tcg.core.agent.tools import create_tools
from tcg.core.agent.workspace import AgentWorkspace

__all__ = ["AgentSession", "AgentWorkspace", "build_system_prompt", "create_tools"]
