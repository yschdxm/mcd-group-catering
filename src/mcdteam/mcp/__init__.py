"""MCP 子包:客户端、错误模型与门店上下文。"""

from .client import DEFAULT_ENDPOINT, MCPClient, RateLimiter, ToolResult
from .context import FulfillmentMode, StoreContext
from .errors import (
    MCPAuthError,
    MCPBusinessError,
    MCPError,
    MCPProtocolError,
    MCPRateLimitError,
    MCPTransportError,
)

__all__ = [
    "DEFAULT_ENDPOINT",
    "MCPClient",
    "RateLimiter",
    "ToolResult",
    "MCPError",
    "MCPAuthError",
    "MCPBusinessError",
    "MCPProtocolError",
    "MCPRateLimitError",
    "MCPTransportError",
    "StoreContext",
    "FulfillmentMode",
]
