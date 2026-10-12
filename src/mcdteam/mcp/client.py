"""麦当劳 MCP Server 的轻量客户端(仅依赖 Python 标准库)。

协议要点(全部经真实调用验证,见 MCP_INTEGRATION.md「接入踩坑实录」):

1. 接入地址 ``https://mcp.mcd.cn``,Streamable HTTP,单端点 POST。
2. 请求头必须带 ``Authorization: Bearer <token>`` 与
   ``Accept: application/json, text/event-stream``,否则可能拿到 406/空响应。
3. 请求体必须是 **UTF-8 编码的字节流**。实测用某些运行时(pwsh 的
   ``Invoke-WebRequest`` 默认编码 / 带 BOM 的字符串)发送时,参数会被服务端
   判定为「缺少参数」并返回 400 —— 这是本项目耗时最久的坑,故在客户端里
   统一用 ``json.dumps(..., ensure_ascii=False).encode("utf-8")`` 显式编码。
4. 响应既可能是普通 JSON,也可能是 SSE(``event: message`` + ``data:``)。
   两种都要能解析。
5. 平台限流 600 次/分钟,超限返回 429。客户端内置滑动窗口限速 + 指数退避。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional

from .errors import (
    MCPAuthError,
    MCPBusinessError,
    MCPError,
    MCPProtocolError,
    MCPRateLimitError,
    MCPTransportError,
)

DEFAULT_ENDPOINT = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "mcd-mcp"


@dataclass
class ToolResult:
    """一次 ``tools/call`` 的结构化结果。"""

    tool: str
    structured: Dict[str, Any] = field(default_factory=dict)
    text: str = ""
    is_error: bool = False
    trace_id: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def data(self) -> Any:
        """业务数据体。多数工具把有效载荷放在 ``data`` 字段。"""
        return self.structured.get("data")

    @property
    def success(self) -> bool:
        return bool(self.structured.get("success", False))

    @property
    def message(self) -> str:
        return str(self.structured.get("message", "") or "")

    def require_data(self) -> Any:
        """校验 ``success`` 后返回 ``data``;失败时抛业务异常。

        这是本项目反复强调的「不要相信 HTTP 200」的地方。
        """
        if not self.success:
            raise MCPBusinessError(
                self.message or "麦当劳 MCP 返回 success=false",
                tool=self.tool,
                code=int(self.structured.get("code") or 0) or None,
                trace_id=self.trace_id,
                payload=self.structured,
            )
        return self.data


class RateLimiter:
    """简单滑动窗口限速器,默认贴合平台 600 次/分钟的限制。"""

    def __init__(self, max_calls: int = 540, window_seconds: float = 60.0) -> None:
        self.max_calls = max(1, max_calls)
        self.window = max(1.0, window_seconds)
        self._marks: Deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._marks and now - self._marks[0] > self.window:
                    self._marks.popleft()
                if len(self._marks) < self.max_calls:
                    self._marks.append(now)
                    return
                sleep_for = self.window - (now - self._marks[0]) + 0.01
            time.sleep(max(0.01, sleep_for))


class MCPClient:
    """麦当劳 MCP Server 客户端。

    Parameters
    ----------
    token:
        麦当劳 MCP Token。**只允许来自环境变量** ``MCD_MCP_TOKEN`` 或显式传参,
        任何情况下都不得写进仓库文件。
    endpoint:
        接入地址,默认 ``https://mcp.mcd.cn``。
    timeout:
        单次请求超时(秒)。
    max_retries:
        针对 429 / 5xx / 超时的重试次数。
    snapshot_dir:
        离线快照目录。请求失败且 ``allow_snapshot=True`` 时从此目录读取录制结果。
    """

    def __init__(
        self,
        token: str,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = 30.0,
        max_retries: int = 3,
        snapshot_dir: Optional[str] = None,
        allow_snapshot: bool = True,
        offline: bool = False,
        rate_limiter: Optional[RateLimiter] = None,
        transport: Optional[Callable[[str, Dict[str, str], bytes, float], tuple]] = None,
        verbose: bool = False,
    ) -> None:
        self.token = (token or "").strip()
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.snapshot_dir = snapshot_dir
        self.allow_snapshot = allow_snapshot
        #: 纯离线模式:不发任何网络请求,所有工具调用直接走快照
        self.offline = offline
        self.limiter = rate_limiter or RateLimiter()
        self.verbose = verbose

        self._id = 0
        self._lock = threading.Lock()
        self._transport = transport or self._http_post
        self.call_log: List[Dict[str, Any]] = []
        #: 记录哪些调用走了离线快照,便于在报告里如实标注数据来源
        self.snapshot_hits: List[str] = []

    # ------------------------------------------------------------------ 传输
    def _http_post(self, url: str, headers: Dict[str, str], body: bytes,
                   timeout: float) -> tuple:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "mcd-group-catering/1.0 (+https://github.com/)",
        }

    # ------------------------------------------------------------ JSON-RPC 层
    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    @staticmethod
    def _decode_body(text: str) -> Dict[str, Any]:
        """兼容普通 JSON 与 SSE 两种响应体。"""
        stripped = text.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            return json.loads(stripped)
        chunks: List[str] = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunks.append(line[5:].strip())
        if not chunks:
            raise MCPProtocolError(f"无法解析的响应体:{text[:200]!r}")
        return json.loads("\n".join(chunks))

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None,
             *, tool: str = "") -> Dict[str, Any]:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            payload["params"] = params
        # 关键:显式 UTF-8 字节流 + 不转义中文
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        if self.offline:
            raise MCPTransportError(
                "离线模式:跳过网络请求", tool=tool or method, payload={"offline": True})

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            started = time.monotonic()
            try:
                status, text = self._transport(self.endpoint, self._headers(), body, self.timeout)
            except urllib.error.HTTPError as exc:  # noqa: PERF203
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:400]
                except Exception:  # pragma: no cover - 读取失败不影响主流程
                    pass
                elapsed = time.monotonic() - started
                self._log(tool or method, status=exc.code, elapsed=elapsed)
                if exc.code in (401, 403):
                    raise MCPAuthError(
                        f"鉴权失败(HTTP {exc.code}) {detail}", tool=tool, code=exc.code
                    ) from exc
                if exc.code == 429:
                    last_error = MCPRateLimitError(
                        f"触发限流(HTTP 429) {detail}", tool=tool, code=429
                    )
                    self._backoff(attempt)
                    continue
                if 500 <= exc.code < 600:
                    last_error = MCPTransportError(
                        f"服务端错误(HTTP {exc.code}) {detail}", tool=tool, code=exc.code
                    )
                    self._backoff(attempt)
                    continue
                raise MCPTransportError(
                    f"请求被拒绝(HTTP {exc.code}) {detail}", tool=tool, code=exc.code
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                elapsed = time.monotonic() - started
                self._log(tool or method, status=None, elapsed=elapsed, error=str(exc))
                last_error = MCPTransportError(f"网络请求失败:{exc}", tool=tool)
                self._backoff(attempt)
                continue

            self._log(tool or method, status=status, elapsed=time.monotonic() - started)
            document = self._decode_body(text)
            if "error" in document and document["error"]:
                error = document["error"]
                raise MCPProtocolError(
                    f"JSON-RPC 错误:{error.get('message', error)}", tool=tool,
                    code=error.get("code"),
                )
            result = document.get("result")
            if not isinstance(result, dict):
                raise MCPProtocolError("响应缺少 result 字段", tool=tool, payload=document)
            return result

        raise last_error or MCPTransportError("未知错误", tool=tool)

    def _backoff(self, attempt: int) -> None:
        if attempt < self.max_retries:
            time.sleep(min(8.0, 0.6 * (2 ** attempt)))

    def _log(self, tool: str, *, status: Optional[int], elapsed: float,
             error: str = "") -> None:
        entry = {"tool": tool, "status": status, "elapsed": round(elapsed, 3)}
        if error:
            entry["error"] = error
        self.call_log.append(entry)
        if self.verbose:
            flag = "ok" if status == 200 and not error else "!!"
            print(f"[mcp] {flag} {tool:<26} {status} {elapsed:.2f}s {error}".rstrip())

    # -------------------------------------------------------------- 快照兜底
    def _snapshot_candidates(self, tool: str, arguments: Dict[str, Any]) -> Iterable[str]:
        """按优先级列出可能的快照文件名。

        * 门店级工具优先匹配带门店编码的文件;
        * 价格与门店券还会额外尝试**带场景后缀**的文件
          (``.<orderType>.<beType>.json``),因为同一门店的到店价与外送价不同,
          只按门店编码命名会让两种场景互相覆盖。
        """
        if not self.snapshot_dir:
            return []
        import os

        store = arguments.get("storeCode") or ""
        order_type = arguments.get("orderType")
        be_type = arguments.get("beType")
        names: List[str] = []
        if store and order_type is not None and be_type is not None:
            names.append(f"{tool}.{store}.{order_type}.{be_type}.json")
        if store:
            names.append(f"{tool}.{store}.json")
        names.append(f"{tool}.json")

        paths = []
        for name in names:
            if not name:
                continue
            path = os.path.join(self.snapshot_dir, name)
            if os.path.exists(path):
                paths.append(path)
        return paths

    def _read_snapshot(self, tool: str, arguments: Dict[str, Any]) -> Optional[ToolResult]:
        for path in self._snapshot_candidates(tool, arguments):
            try:
                # utf-8-sig:快照可能由带 BOM 的编辑器/脚本写出
                with open(path, "r", encoding="utf-8-sig") as handle:
                    payload = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            structured = payload.get("structuredContent", payload)
            if not isinstance(structured, dict):
                continue
            # 快照可能包了一层 JSON-RPC 外壳,统一剥到 structuredContent
            if "result" in structured and isinstance(structured["result"], dict):
                inner = structured["result"].get("structuredContent")
                if isinstance(inner, dict):
                    structured = inner
            self.snapshot_hits.append(f"{tool} <- {path}")
            return ToolResult(
                tool=tool,
                structured=structured,
                text=json.dumps(structured, ensure_ascii=False)[:2000],
                is_error=not structured.get("success", False),
                trace_id=structured.get("traceId"),
                raw=payload,
            )
        return None

    # ------------------------------------------------------------------ 公共 API
    def initialize(self) -> Dict[str, Any]:
        """握手,返回 serverInfo / capabilities。"""
        return self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcd-group-catering", "version": "1.0.0"},
        })

    def list_tools(self) -> List[Dict[str, Any]]:
        """返回服务端当前暴露的全部工具定义。"""
        result = self._rpc("tools/list", {})
        tools = result.get("tools") or []
        return list(tools)

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None,
                  *, use_snapshot: bool = True) -> ToolResult:
        """调用一个工具。

        ``use_snapshot=False`` 用于必须拿实时数据的场景(例如最终下单)。
        """
        arguments = arguments or {}
        try:
            result = self._rpc("tools/call", {"name": name, "arguments": arguments}, tool=name)
        except MCPError:
            if use_snapshot and self.allow_snapshot:
                cached = self._read_snapshot(name, arguments)
                if cached is not None:
                    return cached
            raise

        content = result.get("content") or []
        text = ""
        if content and isinstance(content, list):
            text = "\n".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            # 少数工具只返回文本,这里退化为空结构体但保留 text
            structured = {}
        tool_result = ToolResult(
            tool=name,
            structured=structured,
            text=text,
            is_error=bool(result.get("isError")),
            trace_id=structured.get("traceId"),
            raw=result,
        )
        if not structured and use_snapshot and self.allow_snapshot:
            cached = self._read_snapshot(name, arguments)
            if cached is not None:
                return cached
        return tool_result

    def ensure_ok(self, name: str, arguments: Optional[Dict[str, Any]] = None,
                  *, use_snapshot: bool = True) -> ToolResult:
        """调用并在 ``success=false`` 时抛 :class:`MCPBusinessError`。"""
        result = self.call_tool(name, arguments, use_snapshot=use_snapshot)
        if not result.success:
            raise MCPBusinessError(
                result.message or "MCP 业务失败",
                tool=name,
                code=int(result.structured.get("code") or 0) or None,
                trace_id=result.trace_id,
                payload=result.structured,
            )
        return result

    # ------------------------------------------------------------- 便利封装
    def resolve_store_code(self, city: str, keyword: str = "",
                           be_type: int = 1) -> List[Dict[str, Any]]:
        """按位置搜索门店,返回 ``[{storeCode, storeName, beCode?}]``。

        到店自取(beType=1)不返回 beCode;得来速(beType=5)必带 beCode。
        """
        arguments: Dict[str, Any] = {"beType": be_type, "searchType": 2}
        if city:
            arguments["city"] = city
        if keyword:
            arguments["keyword"] = keyword
        return list(self.ensure_ok("query-nearby-stores", arguments).require_data() or [])

    def resolve_delivery_stores(self, address_id: str, be_type: int = 6) -> List[Dict[str, Any]]:
        """按配送地址解析可配送门店。beType: 2=麦乐送, 6=企业团餐。"""
        return list(self.ensure_ok(
            "delivery-query-stores", {"addressId": address_id, "beType": be_type}
        ).require_data() or [])

    def query_meals(self, store_code: str, order_type: int = 1, be_type: int = 1,
                    be_code: str = "", reservation_date: str = "",
                    *, use_snapshot: bool = True) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {
            "storeCode": store_code, "orderType": order_type, "beType": be_type,
        }
        if be_code:
            arguments["beCode"] = be_code
        if reservation_date:
            arguments["reservationDate"] = reservation_date
        result = self.call_tool("query-meals", arguments, use_snapshot=use_snapshot)
        if not result.success:
            raise MCPBusinessError(
                result.message or "餐品列表查询失败", tool="query-meals",
                code=int(result.structured.get("code") or 0) or None,
                trace_id=result.trace_id, payload=result.structured,
            )
        return result.data or {}

    def calculate_price(self, store_code: str, items: List[Dict[str, Any]],
                        order_type: int = 1, be_type: int = 1, be_code: str = "",
                        reservation_date: str = "", gm_service_code: str = "",
                        *, use_snapshot: bool = True) -> Dict[str, Any]:
        """比价核心。``items[].productCode`` 必须来自 ``query-meals`` 的 ``code``。"""
        arguments: Dict[str, Any] = {
            "storeCode": store_code,
            "orderType": order_type,
            "beType": be_type,
            "items": items,
        }
        if be_code:
            arguments["beCode"] = be_code
        if reservation_date:
            arguments["reservationDate"] = reservation_date
        if gm_service_code:
            arguments["gmServiceCode"] = gm_service_code
        result = self.call_tool("calculate-price", arguments, use_snapshot=use_snapshot)
        if not result.success:
            raise MCPBusinessError(
                result.message or "价格计算失败", tool="calculate-price",
                code=int(result.structured.get("code") or 0) or None,
                trace_id=result.trace_id, payload=result.structured,
            )
        return result.data or {}

    def store_coupons(self, store_code: str, order_type: int = 1, be_type: int = 1,
                      be_code: str = "", *, use_snapshot: bool = True) -> List[Dict[str, Any]]:
        arguments: Dict[str, Any] = {
            "storeCode": store_code, "orderType": order_type, "beType": be_type,
        }
        if be_code:
            arguments["beCode"] = be_code
        return list(self.ensure_ok(
            "query-store-coupons", arguments, use_snapshot=use_snapshot
        ).require_data() or [])

    def store_name(self, store_code: str, *, be_type: int = 1,
                   search_type: int = 1) -> str:
        """尽力查询门店名称。

        为什么需要单独的查询:直接指定 ``--store`` 时不会走门店列表接口,
        因此 ``StoreContext.store_name`` 为空,报告里只能显示编码。
        实测 ``query-nearby-stores(searchType=1)`` 返回的是「收藏餐厅」,
        不一定包含目标门店,所以这里只做**尽力而为**,查不到就返回空串,
        绝不让它影响主流程。
        """
        try:
            records = self.resolve_store_code("", "", be_type=be_type)
        except MCPError:
            return ""
        for record in records:
            if str(record.get("storeCode")) == str(store_code):
                return str(record.get("storeName") or "")
        return ""

    def promotions(self, store_code: str, be_code: str, be_type: int = 6,
                   order_type: int = 2, reservation_date: str = "") -> List[Dict[str, Any]]:
        """企业团餐专属促销规则(满减 / 满折)。仅 beType=6 可用。"""
        arguments: Dict[str, Any] = {
            "storeCode": store_code, "beCode": be_code,
            "orderType": order_type, "beType": be_type,
        }
        if reservation_date:
            arguments["reservationDate"] = reservation_date
        return list(self.ensure_ok("query-promotions", arguments).require_data() or [])

    def campaigns(self, *, use_snapshot: bool = True) -> Dict[str, Any]:
        return self.ensure_ok("campaign-calendar", {}, use_snapshot=use_snapshot).require_data() or {}

    def meal_assistance(self, store_code: str, order_type: int = 2, be_type: int = 6,
                        be_code: str = "") -> List[Dict[str, Any]]:
        """查询团餐助餐服务。

        实测返回结构是 ``data.mealAssistanceItems[]``(不是 ``data`` 本身为数组),
        每项含 ``gmServiceCode`` / ``gmServiceName`` / ``serviceItems`` /
        ``selected`` / ``enable``;不可用的项还会带 ``unusableReason``。
        这里做兼容抽取,避免调用方踩结构差异。
        """
        arguments: Dict[str, Any] = {
            "storeCode": store_code, "orderType": order_type, "beType": be_type,
        }
        if be_code:
            arguments["beCode"] = be_code
        data = self.ensure_ok("query-meal-assistance", arguments).require_data()
        if isinstance(data, dict):
            for key in ("mealAssistanceItems", "items", "list", "services"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            return []
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    def nutrition_raw(self, *, use_snapshot: bool = True) -> str:
        """``list-nutrition-foods`` 返回的是**文本表格**而非 JSON,需要专用解析器。

        该接口实测存在偶发返回空内容的情况(社区 issue #16 亦已记录),
        因此调用方应配合重试与 fixture 兜底。
        """
        result = self.ensure_ok("list-nutrition-foods", {}, use_snapshot=use_snapshot)
        data = result.data
        if isinstance(data, str):
            return data
        if isinstance(data, list) and data:
            return str(data[0])
        return ""

    # ------------------------------------------------------------------ 订单
    def create_order(self, **kwargs: Any) -> Dict[str, Any]:
        """创建订单。

        ⚠️ **这是唯一会产生真实支付链接的调用,后果不可撤销。**

        因此本方法:

        * **绝不使用快照兜底**(``use_snapshot=False``)——下单必须是实时结果;
        * 不做任何自动重试之外的副作用处理,失败即失败;

        调用方(``mcdteam.order``)负责在调用前完成金额校验与用户确认。
        麦当劳 MCP **没有沙箱/测试环境**,不存在"试一单"的路径。
        """
        result = self.ensure_ok("create-order", kwargs, use_snapshot=False)
        return result.data or {}

    def query_order(self, order_id: str) -> Dict[str, Any]:
        """查询订单状态。只读操作,可安全调用。"""
        result = self.ensure_ok("query-order", {"orderId": order_id}, use_snapshot=False)
        return result.data or {}

    def cancel_order(self, order_id: str, cancel_reason_code: int = -1) -> Dict[str, Any]:
        """取消订单。**有副作用**,调用前必须确认。

        取消原因码:1=改主意了 2=重复下单 3=点错了 4=地址电话填错 5=送达时间选错 -1=其它
        """
        result = self.ensure_ok(
            "cancel-order",
            {"orderId": order_id, "cancelReasonCode": int(cancel_reason_code)},
            use_snapshot=False,
        )
        return result.data or {}

    def order_list(self) -> List[Dict[str, Any]]:
        """查询历史订单(只读)。"""
        data = self.ensure_ok("order-list", {}, use_snapshot=False).require_data()
        if isinstance(data, dict):
            return list(data.get("list") or [])
        if isinstance(data, list):
            return list(data)
        return []

    def close(self) -> None:  # pragma: no cover - 为上下文管理器留出扩展点
        return None

    def __enter__(self) -> "MCPClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
