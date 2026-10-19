"""Agent 集合：B.3 落地 dev_agent，B.4 落地 debug_agent，B.5 落地 dev_scripts_agent。

各 agent 只经 :mod:`matrag_agent.tools` 与 :mod:`matrag_agent.clients` 访问外部；
不直连 SDK、不写库、不代签（spec §4.1 红线）。
"""

from __future__ import annotations

from matrag_agent.agents.debug_agent import (
    DebugAgentResult,
    RootCause,
    run_debug_agent,
)
from matrag_agent.agents.dev_agent import DevAgentResult, run_dev_agent
from matrag_agent.agents.dev_scripts_agent import (
    DevScriptsAgentResult,
    run_dev_scripts_agent,
)

#: 已注册 agent（逻辑名 → 模块路径）。
AGENTS: dict[str, str] = {
    "dev_agent": "matrag_agent.agents.dev_agent",
    "debug_agent": "matrag_agent.agents.debug_agent",
    "dev_scripts_agent": "matrag_agent.agents.dev_scripts_agent",
}

__all__ = [
    "AGENTS",
    "DebugAgentResult",
    "DevAgentResult",
    "DevScriptsAgentResult",
    "RootCause",
    "run_debug_agent",
    "run_dev_agent",
    "run_dev_scripts_agent",
]
