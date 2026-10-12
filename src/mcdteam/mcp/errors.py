"""MCP 调用过程中的错误模型。

设计目标:把「网络故障」「令牌问题」「业务拒绝」三类失败区分开,
让上层求解器可以决定是重试、降级到离线快照,还是直接中止并提示用户。
"""

from __future__ import annotations

from typing import Any, Optional


class MCPError(RuntimeError):
    """所有 MCP 相关异常的基类。"""

    #: 面向用户的中文提示,子类可覆盖
    user_hint = "麦当劳 MCP 调用失败"

    def __init__(self, message: str, *, tool: str = "", code: Optional[int] = None,
                 trace_id: Optional[str] = None, payload: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.tool = tool
        self.code = code
        self.trace_id = trace_id
        self.payload = payload

    def __str__(self) -> str:  # pragma: no cover - 便于调试展示
        parts = [self.message]
        if self.tool:
            parts.append(f"tool={self.tool}")
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.trace_id:
            parts.append(f"traceId={self.trace_id}")
        return " | ".join(parts)


class MCPTransportError(MCPError):
    """网络层失败:超时、连接被拒绝、DNS 失败、5xx。"""

    user_hint = "网络异常,请检查本机网络或稍后重试"


class MCPAuthError(MCPError):
    """令牌缺失 / 失效 / 无权限(HTTP 401、403)。"""

    user_hint = "MCP Token 无效或已过期,请到 https://open.mcd.cn/mcp 控制台重新获取"


class MCPRateLimitError(MCPError):
    """触发限流(HTTP 429,平台限制 600 次/分钟)。"""

    user_hint = "调用过于频繁,已触发限流,请降低频率后重试"


class MCPProtocolError(MCPError):
    """响应不是合法 JSON-RPC,或缺 result 字段。"""

    user_hint = "MCP 返回结构异常"


class MCPBusinessError(MCPError):
    """业务侧拒绝,例如商品售罄、门店不可配送、参数不合法。

    注意:麦当劳 MCP 的不少业务错误**不会**抛出 HTTP 错误码,
    而是在 ``structuredContent`` 里返回 ``success=false``。因此这层必须在
    应用层捕获,否则会静默拿到空结果(实测踩坑点,详见 MCP_INTEGRATION.md)。
    """

    user_hint = "业务校验未通过"

    @property
    def is_sold_out(self) -> bool:
        return "售罄" in self.message or "不可售" in self.message

    @property
    def is_missing_param(self) -> bool:
        # 实测:参数名错误时服务端统一回 "缺少参数",而不是指出是哪个参数
        return "缺少参数" in self.message or "不得为空白" in self.message
