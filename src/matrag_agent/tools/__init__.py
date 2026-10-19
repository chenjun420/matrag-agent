"""matrag-agent 工具集：纯函数包装 client / 文件读取，供各 agent 注册调用。"""

from __future__ import annotations

from matrag_agent.tools.binding_spec import get_binding_spec
from matrag_agent.tools.derive import derive
from matrag_agent.tools.method_spec import get_method_spec
from matrag_agent.tools.plan import plan
from matrag_agent.tools.propose_test import propose_test
from matrag_agent.tools.query_exec import query_exec
from matrag_agent.tools.rag_search import rag_search
from matrag_agent.tools.read_bundle import read_bundle

#: 工具注册表（Phase B.1 骨架；agent 侧按名取用）。
#: 说明：B.5 新增的 ``get_method_spec`` / ``get_binding_spec`` 只由
#: ``dev_scripts_agent`` 经 ``_ALLOWED_TOOLS`` 白名单直接导入调用，不并入本注册表
#: （保持 B.1 注册表契约稳定）。
TOOL_REGISTRY: dict[str, object] = {
    "read_bundle": read_bundle,
    "plan": plan,
    "derive": derive,
}

__all__ = [
    "TOOL_REGISTRY",
    "derive",
    "get_binding_spec",
    "get_method_spec",
    "plan",
    "propose_test",
    "query_exec",
    "rag_search",
    "read_bundle",
]
