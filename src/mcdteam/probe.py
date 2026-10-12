"""MCP 能力探针:一次性摸清「哪些工具能调、需要什么参数」。

这个脚本源于本项目开发过程中最耗时的一段排查:

* ``tools/list`` 说需要 ``searchType``/``city`` 的工具,实测传了也报
  ``缺少参数``;
* 只有一个字段名不对,服务端不会告诉你哪个错了,只回一句 ``缺少参数``;
* 请求体的编码方式(pwsh ``Invoke-WebRequest`` 的默认编码 vs 显式 UTF-8 字节流)
  也会让同一份参数在「成功」和 ``缺少参数`` 之间跳变。

把这些结论固化成探针,后来的开发者可以一条命令复现:

    python -m mcdteam.probe --store 1950564

输出两份东西:控制台报告 + ``probe-report.json``(含每步的耗时与原始响应片段)。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Optional

from .config import AppConfig, TOKEN_ENV
from .mcp.client import MCPClient
from .mcp.errors import MCPError

#: 无需门店上下文的工具,可直接用空参数探测
GLOBAL_PROBES: List[tuple] = [
    ("now-time-info", {}, "服务器时间"),
    ("list-nutrition-foods", {}, "营养数据(返回文本表格)"),
    ("query-my-account", {}, "积分账户"),
    ("campaign-calendar", {}, "活动日历"),
    ("available-coupons", {}, "麦麦省可领券"),
    ("query-my-coupons", {"page": "1", "pageSize": "5"}, "卡包券"),
    ("order-list", {}, "历史订单"),
    ("mall-order-list", {}, "商城订单"),
    ("mall-points-products", {}, "积分商城商品"),
    ("query-lottery-info", {}, "积分抽奖活动"),
    ("query-my-prizes", {}, "我的奖品"),
    ("delivery-query-addresses", {}, "配送地址"),
]

#: 需要门店上下文的工具
STORE_PROBES: List[tuple] = [
    ("query-meals", {"orderType": 1, "beType": 1}, "门店菜单"),
    ("query-store-coupons", {"orderType": 1, "beType": 1}, "门店可用券"),
]


def _call(client: MCPClient, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        result = client.call_tool(tool, arguments)
    except MCPError as exc:
        return {"ok": False, "message": str(exc), "elapsed": round(time.monotonic() - started, 3)}
    elapsed = round(time.monotonic() - started, 3)
    structured = result.structured
    return {
        "ok": bool(structured.get("success")),
        "code": structured.get("code"),
        "message": structured.get("message"),
        "elapsed": elapsed,
        "dataType": type(structured.get("data")).__name__,
        "dataPreview": str(structured.get("data"))[:180],
    }


def run_probe(config: AppConfig, store_code: str = "") -> Dict[str, Any]:
    client = MCPClient(config.token, endpoint=config.endpoint,
                       snapshot_dir=config.fixture_dir, allow_snapshot=False)
    report: Dict[str, Any] = {"endpoint": config.endpoint, "results": []}

    initialized = client.initialize()
    report["serverInfo"] = initialized.get("serverInfo")
    tools = client.list_tools()
    report["toolCount"] = len(tools)
    report["tools"] = sorted(t.get("name", "") for t in tools)

    for tool, arguments, label in GLOBAL_PROBES:
        outcome = _call(client, tool, arguments)
        outcome.update({"tool": tool, "label": label, "scope": "global"})
        report["results"].append(outcome)
        _print_line(outcome)

    if store_code:
        for tool, extra, label in STORE_PROBES:
            arguments = {"storeCode": store_code, **extra}
            outcome = _call(client, tool, arguments)
            outcome.update({"tool": tool, "label": label, "scope": "store"})
            report["results"].append(outcome)
            _print_line(outcome)
    return report


def _print_line(outcome: Dict[str, Any]) -> None:
    flag = "✔" if outcome.get("ok") else "✘"
    detail = ""
    if outcome.get("ok"):
        detail = f"{outcome.get('dataType')} {outcome.get('elapsed')}s"
    else:
        detail = f"{outcome.get('message')}"
    print(f"  {flag} {outcome.get('label', ''):<22}{outcome.get('tool', ''):<26}{detail}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mcdteam.probe",
                                     description="麦当劳 MCP 能力探针")
    parser.add_argument("--token", default="", help=f"MCP Token(建议用 {TOKEN_ENV})")
    parser.add_argument("--store", default="", help="用于门店级探测的 storeCode")
    parser.add_argument("--fixtures", default="", help="快照目录")
    parser.add_argument("--out", default="probe-report.json", help="报告输出路径")
    args = parser.parse_args(argv)

    config = AppConfig.load(token=args.token, fixture_dir=args.fixtures)
    if not config.has_token:
        print(f"✘ 未设置 MCP Token。请设置环境变量 {TOKEN_ENV},或用 --token 传入。",
              file=sys.stderr)
        return 2

    print("🔍 麦当劳 MCP 能力探针")
    print("─" * 68)
    report = run_probe(config, store_code=args.store)
    print("─" * 68)
    ok = sum(1 for item in report["results"] if item.get("ok"))
    total = len(report["results"])
    print(f"  成功 {ok}/{total}   暴露工具 {report.get('toolCount')} 个")
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(f"  报告已写入 {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
