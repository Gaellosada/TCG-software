"""Agent module -- Claude CLI-powered assistant for backtesting workflows.

Public API:
- ``CLISession``     — one multi-turn conversation via Claude CLI subprocess
- ``AgentWorkspace`` — on-disk session storage (conversations, assumptions)
- ``cli_available``  — check if the claude binary is on PATH
"""

from tcg.core.agent.session import CLISession, cli_available
from tcg.core.agent.workspace import AgentWorkspace

__all__ = ["CLISession", "AgentWorkspace", "cli_available"]
