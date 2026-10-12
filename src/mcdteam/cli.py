"""命令行入口。

用法速览::

    python -m mcdteam doctor                      # 自检:令牌、工具、快照
    python -m mcdteam plan "14人 预算480 不吃辣 两人减脂 周五12点送到"
    python -m mcdteam plan --store 1950564 --headcount 14 --budget 480 --offline
    python -m mcdteam menu --store 1950564 --role main
    python -m mcdteam tools
    python -m mcdteam leaderboard
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import __version__
from .config import AppConfig, TOKEN_ENV, mask_token
from .data.nutrition import NutritionBook
from .mcp.errors import MCPAuthError, MCPBusinessError, MCPError
from .orchestrator import Session, build_requirements
from .order import (
    CANCEL_REASONS,
    REAL_ORDER_ENV,
    OrderSafetyError,
    build_order_draft,
    describe_order,
    pending_payment_orders,
    submit_order,
    wait_for_payment,
)
from .report.render import render_chat_card, render_markdown, render_terminal
from .solve.constraints import CateringRequirements, parse_requirements


def _emit(message: str) -> None:
    print(f"  … {message}", file=sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcdteam",
        description="麦麦团餐统筹官 —— 基于麦当劳 MCP 的企业团餐统筹引擎",
    )
    parser.add_argument("--version", action="version", version=f"mcdteam {__version__}")
    parser.add_argument("--token", default="", help=f"麦当劳 MCP Token(建议用环境变量 {TOKEN_ENV})")
    parser.add_argument("--store", default="", help="门店 storeCode")
    parser.add_argument("--city", default="", help="城市名")
    parser.add_argument("--be-code", default="", help="业务编码(团餐/外送/得来速必传)")
    parser.add_argument("--address-id", default="", help="配送地址 ID")
    parser.add_argument("--fixtures", default="", help="离线快照目录")
    parser.add_argument("--endpoint", default="", help="MCP 接入地址")
    parser.add_argument("--offline", action="store_true", help="完全离线:只用快照,不发网络请求")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出每步调用日志")

    sub = parser.add_subparsers(dest="command")

    plan = sub.add_parser("plan", help="根据一句需求生成团餐采购方案")
    plan.add_argument("text", nargs="*", help="自然语言需求,例如「14人 预算480 不吃辣」")
    plan.add_argument("--headcount", type=int, help="人数")
    plan.add_argument("--budget", type=float, help="总预算(元)")
    plan.add_argument("--per-person", type=float, help="人均预算(元)")
    plan.add_argument("--mode", help="pickup / drive_thru / delivery / group")
    plan.add_argument("--at", dest="reserve_at", help="送达或取餐时间,如 12:30 或 2026-10-16 12:30")
    plan.add_argument("--avoid", action="append", default=[],
                      help="硬性忌口,可重复:spicy/beef/pork/fish/chicken/egg/dairy/vegetarian")
    plan.add_argument("--prefer", action="append", default=[],
                      help="软性偏好,可重复:low_calorie/high_protein/low_sodium/filling/"
                           "shareable/coffee/dessert/refreshing")
    plan.add_argument("--max-kcal", type=float, help="人均热量上限(kcal)")
    plan.add_argument("--min-protein", type=float, help="人均蛋白质下限(g)")
    plan.add_argument("--effort", type=int, default=48, help="搜索强度(重启次数),默认 48")
    plan.add_argument("--max-items", type=int, default=0, help="只取前 N 个餐品(调试用)")
    plan.add_argument("--no-verify", action="store_true", help="跳过服务端价格回算")
    plan.add_argument("--json", dest="json_path", help="把方案导出为 JSON")
    plan.add_argument("--markdown", dest="md_path", help="把方案导出为 Markdown")
    plan.add_argument("--no-color", action="store_true", help="终端输出不上色")

    doctor = sub.add_parser("doctor", help="自检:MCP 连通性、工具可用性、快照完整性")
    doctor.add_argument("--skip-live", action="store_true", help="只检查本地快照,不联网")

    menu = sub.add_parser("menu", help="查看门店菜单/取价/营养覆盖情况")
    menu.add_argument("--role", help="只看某个角色:main/side/drink/dessert/combo/breakfast")
    menu.add_argument("--limit", type=int, default=30, help="展示条数,默认 30")
    menu.add_argument("--max-items", type=int, default=0, help="只取前 N 个餐品")
    menu.add_argument("--json", dest="json_path", help="导出菜单为 JSON")
    menu.add_argument("--sort", choices=["price", "protein", "kcal"], default="price",
                      help="排序字段,默认 price")

    sub.add_parser("tools", help="列出 MCP Server 暴露的全部工具")

    probe = sub.add_parser("probe", help="运行 MCP 能力探针,输出可复现的可用性报告")
    probe.add_argument("--out", default="probe-report.json", help="报告输出路径")

    scan = sub.add_parser("scan", help="提交前扫描仓库中是否残留真实凭据")
    scan.add_argument("--path", default=".", help="扫描根目录,默认当前目录")

    lb = sub.add_parser("leaderboard", help="抓取比赛 Issue 区的项目 Star 排行")
    lb.add_argument("--pages", type=int, default=6, help="抓取页数,每页 100 条")
    lb.add_argument("--limit", type=int, default=25, help="展示前 N 名")

    # Agent 专用入口:stdout 只输出一个 JSON 信封,便于程序化消费
    ap = sub.add_parser("agent-plan",
                        help="Agent 入口:输出统一 JSON 信封,失败也返回结构化错误")
    ap.add_argument("text", nargs="*", help="自然语言需求")
    ap.add_argument("--headcount", type=int)
    ap.add_argument("--budget", type=float)
    ap.add_argument("--per-person", type=float)
    ap.add_argument("--mode")
    ap.add_argument("--at", dest="reserve_at")
    ap.add_argument("--avoid", action="append", default=[])
    ap.add_argument("--prefer", action="append", default=[])
    ap.add_argument("--max-kcal", type=float)
    ap.add_argument("--min-protein", type=float)
    ap.add_argument("--effort", type=int, default=48)
    ap.add_argument("--max-items", type=int, default=0)
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--json", dest="json_path", help="同时把信封写入该文件")

    # 下单:默认 dry-run,真实下单需要 --pay + 环境变量双闸门
    order = sub.add_parser("order", help="下单相关操作(默认 dry-run,不会真实下单)")
    order_sub = order.add_subparsers(dest="order_action", required=True)

    for name, help_text in (("preview", "装配下单请求但不提交(dry-run)"),
                            ("pay", "真实创建订单 —— 需要 --pay 且设置 "
                                    f"{REAL_ORDER_ENV}=1")):
        node = order_sub.add_parser(name, help=help_text)
        node.add_argument("text", nargs="*", help="自然语言需求")
        node.add_argument("--headcount", type=int)
        node.add_argument("--budget", type=float)
        node.add_argument("--per-person", type=float)
        node.add_argument("--mode")
        node.add_argument("--at", dest="reserve_at")
        node.add_argument("--avoid", action="append", default=[])
        node.add_argument("--prefer", action="append", default=[])
        node.add_argument("--max-kcal", type=float)
        node.add_argument("--min-protein", type=float)
        node.add_argument("--effort", type=int, default=48)
        node.add_argument("--max-items", type=int, default=0)
        node.add_argument("--no-verify", action="store_true")
        node.add_argument("--take-way", default="",
                          help="到店/得来速场景的取餐方式码,如 eat-in / take-in-store")
        node.add_argument("--remark", default="", help="订单备注(≤50 字,仅外送场景)")
        node.add_argument("--tableware", type=lambda v: str(v).lower() in ("1", "true", "yes"),
                          default=None, help="是否需要餐具")
        node.add_argument("--no-color", action="store_true")
        node.add_argument("--json", dest="json_path")
        if name == "pay":
            node.add_argument("--pay", action="store_true",
                              help="显式确认真实下单(还需环境变量闸门)")
            node.add_argument("--allow-amount-mismatch", action="store_true",
                              help="金额超出容差时仍提交(谨慎)")
            node.add_argument("--allow-delivery-mismatch", action="store_true",
                              help="门店不在该地址配送范围时仍提交(通常会失败)")
            node.add_argument("--share", default="",
                              help="把可转发到群里的支付卡片写入该文件")
            node.add_argument("--share-format", default="auto",
                              choices=["auto", "text", "md", "json"],
                              help="支付卡片格式;auto 按 --share 的扩展名推断")

    st = order_sub.add_parser("status", help="查询订单状态(只读)")
    st.add_argument("--order-id", required=True)
    st.add_argument("--json", dest="json_path")

    cn = order_sub.add_parser("cancel", help="取消订单(有副作用)")
    cn.add_argument("--order-id", required=True)
    cn.add_argument("--reason-code", type=int, default=-1,
                    help="取消原因码:" + "、".join(
                        f"{k}={v}" for k, v in sorted(CANCEL_REASONS.items())))
    cn.add_argument("--json", dest="json_path")

    order_sub.add_parser("list", help="查询历史订单(只读)")

    cmp_parser = sub.add_parser("compare",
                                help="多门店比价:同一地址下哪家最便宜(只读)")
    cmp_parser.add_argument("text", nargs="*", help="自然语言需求")
    cmp_parser.add_argument("--headcount", type=int)
    cmp_parser.add_argument("--budget", type=float)
    cmp_parser.add_argument("--per-person", type=float)
    cmp_parser.add_argument("--mode", default="delivery",
                            help="delivery(麦乐送)或 group(企业团餐),默认 delivery")
    cmp_parser.add_argument("--at", dest="reserve_at")
    cmp_parser.add_argument("--avoid", action="append", default=[])
    cmp_parser.add_argument("--prefer", action="append", default=[])
    cmp_parser.add_argument("--max-kcal", type=float)
    cmp_parser.add_argument("--min-protein", type=float)
    cmp_parser.add_argument("--effort", type=int, default=24,
                            help="每家门店的搜索强度,默认 24(比价跑多家,调低控耗时)")
    cmp_parser.add_argument("--max-stores", type=int, default=4,
                            help="最多比价几家门店,默认 4")
    cmp_parser.add_argument("--no-verify", action="store_true")
    cmp_parser.add_argument("--no-color", action="store_true")
    cmp_parser.add_argument("--json", dest="json_path")
    cmp_parser.add_argument("--markdown", dest="md_path",
                            help="把比价表导出为 Markdown")

    cl = order_sub.add_parser("cleanup", help="列出并清理被遗忘的未支付订单")
    cl.add_argument("--cancel", action="store_true",
                    help="真正取消;不加则只列出(默认 dry-run)")
    cl.add_argument("--reason-code", type=int, default=1,
                    help="取消原因码,默认 1=改主意了")
    cl.add_argument("--yes", action="store_true", help="跳过确认")
    cl.add_argument("--older-than", type=float, default=0.0,
                    help="只处理创建时间早于 N 分钟前的订单,默认 0(全部)")
    cl.add_argument("--json", dest="json_path")

    wt = order_sub.add_parser("wait", help="轮询订单状态直到已支付/终态(只读)")
    wt.add_argument("--order-id", required=True)
    wt.add_argument("--timeout", type=float, default=180.0,
                    help="最长等待秒数,默认 180")
    wt.add_argument("--interval", type=float, default=5.0,
                    help="轮询间隔秒数,默认 5")
    wt.add_argument("--json", dest="json_path")

    return parser


# --------------------------------------------------------------------- doctor
def cmd_doctor(args: argparse.Namespace, config: AppConfig) -> int:
    print("🩺 麦麦团餐统筹官 · 自检")
    print("─" * 68)
    print(f"  版本            {__version__}")
    print(f"  接入地址        {config.endpoint}")
    print(f"  Token           {mask_token(config.token)}")
    print(f"  快照目录        {config.fixture_dir}")
    print(f"  运行模式        {'离线(无网络请求)' if config.offline else '在线'}")
    print("─" * 68)

    fixtures = [
        ("nutrition_raw.txt", "营养数据快照"),
        ("query-meals.1950564.json", "门店菜单快照"),
        ("price-map.store1950564.json", "门店价格快照"),
        ("query-store-coupons.1950564.json", "门店券快照"),
    ]
    print("  📦 本地快照")
    missing = 0
    for name, label in fixtures:
        path = os.path.join(config.fixture_dir, name)
        exists = os.path.exists(path)
        size = f"{os.path.getsize(path) / 1024:.1f} KB" if exists else "缺失"
        print(f"     {'✔' if exists else '✘'} {label:<16} {name:<34} {size}")
        missing += 0 if exists else 1

    # 统计仓库里实际抓到多少家门店的快照(比价与多场景离线演示都依赖它们)
    try:
        import glob
        meal_files = glob.glob(os.path.join(config.fixture_dir, "query-meals.*.json"))
        scoped_prices = glob.glob(os.path.join(
            config.fixture_dir, "price-map.store*.*.*.json"))
        address_files = glob.glob(os.path.join(
            config.fixture_dir, "delivery-query-stores.*.json"))
        store_codes = sorted({
            os.path.basename(path).split(".")[1]
            for path in meal_files
            if len(os.path.basename(path).split(".")) >= 3
        })
        if store_codes:
            print(f"     ℹ 已抓取门店快照 {len(store_codes)} 家:"
                  f"{', '.join(store_codes)}")
        if scoped_prices:
            print(f"     ℹ 带场景后缀的价格快照 {len(scoped_prices)} 个"
                  "(到店价与外送价不会互相覆盖)")
        if address_files:
            print(f"     ℹ 可配送门店列表快照 {len(address_files)} 份"
                  "(离线也能解析店名与 beCode)")
    except OSError:
        pass

    # 营养快照可解析性
    try:
        nutrition_path = os.path.join(config.fixture_dir, "nutrition_raw.txt")
        if os.path.exists(nutrition_path):
            with open(nutrition_path, "r", encoding="utf-8") as handle:
                book = NutritionBook.from_raw(handle.read())
            print(f"     └ 解析成功:{len(book)} 条营养记录")
    except Exception as exc:  # noqa: BLE001
        print(f"     └ ✘ 营养快照解析失败:{exc}")

    if config.offline or args.skip_live:
        print("─" * 68)
        print(f"  结果:本地检查完成,缺失 {missing} 项")
        return 0 if missing == 0 else 1

    if not config.has_token:
        print("─" * 68)
        print(f"  ✘ 未设置 MCP Token。请设置环境变量 {TOKEN_ENV},或用 --token 传入。")
        return 2

    print("─" * 68)
    print("  🌐 在线检查")
    session = Session(config, progress=_emit if config.verbose else None)
    try:
        info = session.client.initialize()
        server = info.get("serverInfo", {})
        print(f"     ✔ 握手成功:{server.get('name')} v{server.get('version')}")
        tools = session.client.list_tools()
        print(f"     ✔ 工具列表:{len(tools)} 个")
        store_only = [t["name"] for t in tools
                      if any(k in json.dumps(t.get("inputSchema", {}))
                             for k in ("storeCode", "addressId"))]
        print(f"       └ 其中门店/地址相关:{len(store_only)} 个")
    except MCPAuthError as exc:
        print(f"     ✘ 鉴权失败:{exc}")
        return 2
    except MCPError as exc:
        print(f"     ✘ 连接失败:{exc}")
        return 3

    checks = [
        ("now-time-info", {}, "服务器时间"),
        ("list-nutrition-foods", {}, "营养数据"),
        ("query-my-account", {}, "积分账户"),
        ("campaign-calendar", {}, "活动日历"),
    ]
    for tool, tool_args, label in checks:
        try:
            result = session.client.call_tool(tool, tool_args)
            flag = "✔" if result.success else "✘"
            detail = "" if result.success else f" ({result.message})"
            print(f"     {flag} {label:<10} {tool}{detail}")
        except MCPError as exc:
            print(f"     ✘ {label:<10} {tool} ({exc})")

    if config.store_code:
        print("─" * 68)
        print(f"  🏪 门店检查 storeCode={config.store_code}")
        try:
            payload = session.client.query_meals(config.store_code)
            codes = {str(m.get("code")) for g in payload.get("categories", []) or []
                     for m in g.get("meals", []) or []}
            print(f"     ✔ query-meals:返回 {len(codes)} 个可售编码")
        except MCPBusinessError as exc:
            print(f"     ✘ query-meals:{exc.message}")
    else:
        print("     (未指定 --store,跳过门店级检查)")

    print("─" * 68)
    print("  结果:自检完成")
    return 0


# ----------------------------------------------------------------------- plan
def cmd_plan(args: argparse.Namespace, config: AppConfig) -> int:
    text = " ".join(args.text or [])
    requirements = build_requirements(text, **_plan_overrides(args))
    if not config.offline and not config.has_token:
        print(f"✘ 未设置 MCP Token。请设置环境变量 {TOKEN_ENV},或用 --token 传入。",
              file=sys.stderr)
        return 2

    if requirements.headcount <= 0:
        print("✘ 人数必须大于 0", file=sys.stderr)
        return 2

    session = Session(config, progress=_emit if not args.no_color else None)
    try:
        if config.verbose:
            print(f"  [需求解析] {json.dumps(requirements.to_dict(), ensure_ascii=False)}",
                  file=sys.stderr)
        plan = session.plan(requirements, verify=not args.no_verify)
    except MCPAuthError as exc:
        print(f"✘ 鉴权失败:{exc.user_hint}", file=sys.stderr)
        return 2
    except MCPBusinessError as exc:
        print(f"✘ 麦当劳 MCP 业务拒绝:{exc.message}", file=sys.stderr)
        if exc.is_missing_param:
            print("  提示:参数不完整。门店级工具需要有效的 storeCode,"
                  "团餐/外送/得来速还需要 beCode。", file=sys.stderr)
        return 3
    except MCPError as exc:
        print(f"✘ 调用失败:{exc}", file=sys.stderr)
        return 3

    print(render_terminal(plan, requirements, color=not args.no_color,
                          source_note=session.snapshot_note()))

    if plan.lines:
        print(render_chat_card(plan, requirements))
        print()

    if args.md_path:
        import datetime as _dt
        content = render_markdown(
            plan, requirements, source_note=session.snapshot_note(),
            generated_at=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            context=session.trace_dict())
        with open(args.md_path, "w", encoding="utf-8") as handle:
            handle.write(content)
        print(f"  ✔ Markdown 已写入 {args.md_path}", file=sys.stderr)

    if args.json_path:
        payload = {
            "requirements": requirements.to_dict(),
            "plan": plan.to_dict(),
            "context": session.trace_dict(),
        }
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f"  ✔ JSON 已写入 {args.json_path}", file=sys.stderr)

    return 0 if plan.lines else 1


def agent_plan(args: argparse.Namespace, config: AppConfig) -> Dict[str, Any]:
    """供 Agent 调用的方案生成:永远返回结构化字典,绝不抛异常、绝不打印终端卡片。

    与 :func:`cmd_plan` 的区别:

    * **单一契约**:无论成功失败都返回同一个信封结构,Agent 只需判断
      ``status`` 与 ``ok``,不需要解析人类可读文本;
    * **失败也带修复提示**:``error.hint`` 里给出下一步该做什么
      (例如 Token 无效就提示去哪里申请);
    * **不走终端渲染**:终端卡片是给人看的,Agent 读 JSON 即可。

    这是 `scripts/plan.py` 的实现核心,保证脚本与库共用同一段逻辑。
    """
    text = " ".join(args.text or [])
    requirements = build_requirements(text, **_plan_overrides(args))
    envelope: Dict[str, Any] = {
        "status": "error",
        "ok": False,
        "command": "plan",
        "input": text,
        "error": None,
        "plan": None,
        "requirements": requirements.to_dict(),
        "context": None,
    }

    if requirements.headcount <= 0:
        envelope["error"] = {
            "type": "invalid_input",
            "message": "人数必须大于 0",
            "hint": "请向用户追问本单人数,再用 --headcount 传入",
        }
        return envelope

    if not config.offline and not config.has_token:
        envelope["error"] = {
            "type": "missing_token",
            "message": f"未设置麦当劳 MCP Token(环境变量 {TOKEN_ENV})",
            "hint": ("到 https://open.mcd.cn/mcp 登录后激活 Token,"
                     "然后设置环境变量 MCD_MCP_TOKEN;"
                     "或先用 --offline 跑内置快照演示"),
        }
        return envelope

    session = Session(config)
    try:
        plan = session.plan(requirements, verify=not args.no_verify)
    except MCPAuthError as exc:
        envelope["error"] = {
            "type": "auth_failed",
            "message": str(exc.message),
            "hint": "Token 无效或过期,请到 https://open.mcd.cn/mcp 控制台重新获取",
        }
        return envelope
    except MCPBusinessError as exc:
        envelope["error"] = {
            "type": "business_rejected",
            "message": str(exc.message),
            "tool": exc.tool,
            "traceId": exc.trace_id,
            "hint": ("参数与门店不匹配时麦当劳会返回「缺少参数」。请确认:"
                     "① --store 是真实存在的门店编码;"
                     "② 团餐/外送/得来速场景需要 beCode;"
                     "③ 企业团餐还需要 gmServiceCode。"
                     "详见 references/troubleshooting.md"),
        }
        return envelope
    except MCPError as exc:
        envelope["error"] = {
            "type": "mcp_error",
            "message": str(exc),
            "tool": getattr(exc, "tool", ""),
            "hint": "检查网络与 Token;或先用 --offline 验证脚本本身可用",
        }
        return envelope

    if not plan.lines:
        envelope["error"] = {
            "type": "empty_plan",
            "message": "求解结果为空,未生成任何采购项",
            "hint": ("通常是预算过低或忌口过滤后无可用餐品。"
                     "可提高预算、减少 --avoid 条件,或提高 --effort 重试"),
        }
        return envelope

    envelope["status"] = "ok"
    envelope["ok"] = True
    envelope["plan"] = plan.to_dict()
    envelope["context"] = session.trace_dict()
    envelope["meta"] = {
        "offline": config.offline,
        "verifiedByApi": plan.verified_by_api,
        "source": session.snapshot_note(),
        "storeDescription": session.store_context.describe() if session.store_context else "",
        "chartCard": render_chat_card(plan, requirements),
    }
    if not plan.verified_by_api:
        envelope["meta"]["notice"] = (
            "本次金额为本地按单价估算,未经 calculate-price 回算,"
            "必须向用户说明金额为预估"
        )
    return envelope


# ------------------------------------------------------------------- 下单
def _plan_and_session(args: argparse.Namespace, config: AppConfig):
    """复用 agent_plan 的求解结果,同时把 Session 带出来供下单使用。

    下单需要 ``Session.store_context``(storeCode/beCode/gmServiceCode/addressId),
    这些字段不在 JSON 信封里,因此这里重新执行一遍求解并返回内部对象。

    :raises MCPError: 求解失败时向上抛,由调用方转成结构化输出。
    """
    text = " ".join(getattr(args, "text", None) or [])
    requirements = build_requirements(text, **_plan_overrides(args))
    if requirements.headcount <= 0:
        raise MCPBusinessError("人数必须大于 0", tool="plan")
    if not config.offline and not config.has_token:
        raise MCPAuthError(f"未设置麦当劳 MCP Token(环境变量 {TOKEN_ENV})", tool="plan")
    session = Session(config)
    plan = session.plan(requirements, verify=not getattr(args, "no_verify", False))
    return session, plan, requirements


def _resolve_share_format(path: str, requested: str = "auto") -> str:
    """决定支付卡片的输出格式。

    ``auto`` 时按文件扩展名推断:``.md`` → markdown,``.json`` → json,其余按纯文本。
    """
    if requested and requested != "auto":
        return requested
    lowered = (path or "").lower()
    if lowered.endswith(".md") or lowered.endswith(".markdown"):
        return "md"
    if lowered.endswith(".json"):
        return "json"
    return "text"


def _cmd_order_cleanup(args: argparse.Namespace, config: AppConfig) -> int:
    """列出并（可选）取消被遗忘的未支付订单。

    团餐场景很容易积累废单:方案排好、订单创建了,结果预算没批下来或临时换店,
    订单就挂在那儿。虽然未支付订单会自动过期,但混在列表里影响排查。

    **默认只列出,不取消**（dry-run）。真正取消需要 ``--cancel``。
    """
    import datetime as _dt

    session = Session(config)
    try:
        pending = pending_payment_orders(session.client)
    except MCPError as exc:
        print(f"✘ 查询历史订单失败:{exc}", file=sys.stderr)
        return 3

    if args.older_than:
        cutoff = _dt.datetime.now() - _dt.timedelta(minutes=args.older_than)
        kept = []
        for order in pending:
            try:
                created = _dt.datetime.strptime(
                    str(order.get("createTime") or ""), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                kept.append(order)      # 时间解析不了就不筛掉,交给用户判断
                continue
            if created <= cutoff:
                kept.append(order)
        pending = kept

    if not pending:
        print("✔ 没有未支付订单,无需清理")
        return 0

    print(f"发现 {len(pending)} 笔未支付订单")
    print("─" * 78)
    print(f"{'订单号':<32}{'门店':<24}{'应付':>8}  创建时间")
    print("─" * 78)
    for order in pending:
        store = str(order.get("storeName") or order.get("storeCode") or "")[:22]
        amount = order.get("realTotalAmount") or order.get("totalAmount") or "0"
        print(f"{str(order.get('orderId', '')):<32}{store:<24}"
              f"¥{amount:>7}  {order.get('createTime', '')}")
    print("─" * 78)

    if not args.cancel:
        print("  ⏸ dry-run:未取消任何订单")
        print(f"  要真正取消请加 --cancel（原因码 {args.reason_code}="
              f"{CANCEL_REASONS.get(args.reason_code, '其它')}）")
        print("  ℹ 未支付订单即便不取消也会自动过期关闭,不会产生扣款")
        if args.json_path:
            with open(args.json_path, "w", encoding="utf-8") as handle:
                json.dump({"dryRun": True, "orders": pending}, handle,
                          ensure_ascii=False, indent=2, default=str)
        return 0

    if not args.yes:
        try:
            answer = input(f"确认取消这 {len(pending)} 笔未支付订单?[y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes", "是"):
            print("已取消操作,未做任何改动")
            return 0

    cancelled, failed = [], []
    for order in pending:
        order_id = str(order.get("orderId", ""))
        try:
            session.client.cancel_order(order_id, args.reason_code)
            cancelled.append(order_id)
            print(f"  ✔ 已取消 {order_id}")
        except MCPError as exc:
            failed.append((order_id, exc.message))
            print(f"  ✘ 取消失败 {order_id}:{exc.message}", file=sys.stderr)

    print("─" * 78)
    print(f"  结果:成功取消 {len(cancelled)} 笔,失败 {len(failed)} 笔")
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump({"dryRun": False, "cancelled": cancelled,
                       "failed": [{"orderId": o, "message": m} for o, m in failed]},
                      handle, ensure_ascii=False, indent=2, default=str)
    return 0 if not failed else 1


def cmd_order(args: argparse.Namespace, config: AppConfig) -> int:
    """下单相关子命令。

    **安全说明**:麦当劳 MCP 没有沙箱环境,``create-order`` 会真实产生待支付订单。
    因此 ``preview`` 恒为 dry-run;``pay`` 需要同时满足
    ``--pay`` + 环境变量 ``MCD_ALLOW_REAL_ORDER=1``。
    """
    action = args.order_action

    if action == "cleanup":
        return _cmd_order_cleanup(args, config)

    if action == "wait":
        session = Session(config)
        print(f"⏳ 轮询订单 {args.order_id}(最长 {args.timeout:g} 秒,"
              f"每 {args.interval:g} 秒一次)")
        result = wait_for_payment(
            session.client, args.order_id,
            timeout=args.timeout, interval=args.interval,
            progress=lambda m: print(f"   {m}"))
        if result.get("error"):
            print(f"✘ 查询失败:{result['error']}", file=sys.stderr)
            return 3
        print("─" * 68)
        print(describe_order(result.get("last") or {}))
        print("─" * 68)
        if result["paid"]:
            print("  ✅ 已支付,订单进入处理流程")
        elif result["cancelled"]:
            print("  ⛔ 订单已取消")
        elif result["timedOut"]:
            print(f"  ⏰ 等待 {args.timeout:g} 秒后仍未支付"
                  f"(查询 {result['polls']} 次)。可稍后用 "
                  f"order status --order-id {args.order_id} 再查")
        else:
            print(f"  当前状态:{result.get('orderStatus') or '未知'}")
        if args.json_path:
            with open(args.json_path, "w", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2, default=str)
        return 0

    if action == "list":
        session = Session(config)
        try:
            orders = session.client.order_list()
        except MCPError as exc:
            print(f"✘ 查询历史订单失败:{exc}", file=sys.stderr)
            return 3
        if not orders:
            print("暂无历史订单")
            return 0
        print(f"{'订单号':<32}{'类型':<8}{'状态':<14}{'下单时间'}")
        print("─" * 78)
        for item in orders:
            kind = "外送" if str(item.get("orderType")) == "2" else "到店"
            print(f"{str(item.get('orderId', '')):<32}{kind:<8}"
                  f"{str(item.get('orderStatusText') or item.get('orderStatus') or ''):<14}"
                  f"{item.get('createTime', '')}")
        return 0

    if action in ("status", "cancel"):
        if not args.order_id:
            print("✘ 需要 --order-id", file=sys.stderr)
            return 2
        session = Session(config)
        try:
            if action == "status":
                data = session.client.query_order(args.order_id)
            else:
                data = session.client.cancel_order(args.order_id, args.reason_code)
        except MCPError as exc:
            print(f"✘ {'查询' if action == 'status' else '取消'}订单失败:{exc}",
                  file=sys.stderr)
            return 3
        print(describe_order(data) if action == "status"
              else f"✔ 已取消订单 {args.order_id}"
                   f"(原因:{CANCEL_REASONS.get(args.reason_code, '其它')})")
        if args.json_path:
            with open(args.json_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
        return 0

    # ------------------------------------------------------ preview / pay
    if not config.has_token:
        print(f"✘ 下单需要麦当劳 MCP Token(环境变量 {TOKEN_ENV});"
              "离线模式只能生成方案,无法下单", file=sys.stderr)
        return 2
    try:
        session, plan, requirements = _plan_and_session(args, config)
    except MCPBusinessError as exc:
        print(f"✘ {exc.message}", file=sys.stderr)
        return 2
    except MCPError as exc:
        print(f"✘ 生成方案失败:{exc}", file=sys.stderr)
        return 3

    if not plan.lines:
        print("✘ 方案为空,无法下单", file=sys.stderr)
        return 1
    if not session.store_context:
        print("✘ 缺少门店上下文,无法下单", file=sys.stderr)
        return 1

    dry_run = action != "pay" or not args.pay
    try:
        draft = build_order_draft(
            session.client, session.store_context, plan,
            take_way=args.take_way, remark=args.remark,
            need_tableware=args.tableware, dry_run=dry_run,
            progress=_emit if config.verbose else None,
        )
    except (OrderSafetyError, MCPError) as exc:
        print(f"✘ 装配下单请求失败:{exc}", file=sys.stderr)
        return 3

    print(render_terminal(plan, requirements, color=not args.no_color,
                          source_note=session.snapshot_note()))
    print("─" * 68)
    print("🧾 下单请求装配")
    print("─" * 68)
    print(draft.summary())
    if draft.take_way_options:
        print("\n  可选取餐方式:" + "、".join(
            f"{item.get('code')}({item.get('title', '')})" for item in draft.take_way_options))
    for note in draft.notes:
        print(f"  · {note}")
    for warning in draft.warnings:
        print(f"  ⚠ {warning}")

    if not dry_run:
        draft = submit_order(session.client, draft, allow_real=args.pay,
                             allow_amount_mismatch=args.allow_amount_mismatch,
                             allow_delivery_mismatch=args.allow_delivery_mismatch)

    print("─" * 68)
    if draft.submitted:
        print(f"  ✅ 订单已创建:{draft.order_id}")
        if draft.order_status:
            print(f"  状态:{draft.order_status}")
        if draft.expire_pay_time:
            print(f"  ⏰ 请在 {draft.expire_pay_time} 前完成支付,逾期订单自动关闭(不产生扣款)")
        if draft.payment_url:
            print(f"  💳 支付链接:{draft.payment_url}")
        else:
            print("  💳 未返回支付链接,请打开麦当劳 APP 在「我的订单」中支付")
        if draft.pickup_code:
            print(f"  🔢 取餐码:{draft.pickup_code}")
        reminder = draft.pickup_reminder()
        if reminder:
            print(f"  ℹ {reminder}")
        print()
        fmt = _resolve_share_format(args.share, args.share_format)
        print(f"  📤 可转发到群里的支付卡片({fmt}):")
        print("─" * 68)
        for line in draft.render_card(fmt).splitlines():
            print(f"  {line}")
        print("─" * 68)
        if getattr(args, "share", ""):
            try:
                with open(args.share, "w", encoding="utf-8") as handle:
                    handle.write(draft.render_card(fmt) + "\n")
                print(f"  ✔ 支付卡片({fmt})已写入 {args.share}", file=sys.stderr)
            except OSError as exc:
                print(f"  ⚠ 无法写入 {args.share}:{exc}", file=sys.stderr)
        print(f"  查询状态:order status --order-id {draft.order_id}")
        print(f"  撤销订单:order cancel --order-id {draft.order_id} --reason-code 3")
    elif draft.error:
        print(f"  ✘ 下单失败:{draft.error['message']}")
        if draft.error.get("traceId"):
            print(f"    traceId:{draft.error['traceId']}")
    else:
        print("  ⏸ 未下单(dry-run 或安全闸门未放行)")
        print("  要真实下单需同时满足:传 --pay 且设置环境变量 "
              f"{REAL_ORDER_ENV}=1")
        print("  ⚠ 麦当劳 MCP 没有测试环境,下单会产生真实待支付订单")

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump({"plan": plan.to_dict(), "order": draft.to_dict()},
                      handle, ensure_ascii=False, indent=2, default=str)
        print(f"  ✔ JSON 已写入 {args.json_path}", file=sys.stderr)
    return 0


def _plan_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    avoid_map = {
        "spicy": "avoid_spicy", "beef": "avoid_beef", "pork": "avoid_pork",
        "fish": "avoid_fish", "chicken": "avoid_chicken", "egg": "avoid_egg",
        "dairy": "avoid_dairy", "vegetarian": "vegetarian",
    }
    overrides: Dict[str, Any] = {
        "headcount": getattr(args, "headcount", None),
        "budget_total": getattr(args, "budget", None),
        "budget_per_person": getattr(args, "per_person", None),
        "mode": getattr(args, "mode", None),
        "reserve_at": getattr(args, "reserve_at", None),
        "max_kcal_per_person": getattr(args, "max_kcal", None),
        "min_protein_per_person": getattr(args, "min_protein", None),
        "effort": getattr(args, "effort", None),
        "store_code": getattr(args, "store", "") or None,
        "city": getattr(args, "city", "") or None,
    }
    for flag in getattr(args, "avoid", []) or []:
        key = avoid_map.get(flag.lower().replace("-", "_"))
        if key:
            overrides[key] = True
    preferences = [p.lower().replace("-", "_") for p in getattr(args, "prefer", []) or []]
    if preferences:
        overrides["preferences"] = preferences
    return overrides


# ----------------------------------------------------------------------- menu
def cmd_menu(args: argparse.Namespace, config: AppConfig) -> int:
    if not config.offline and not config.has_token:
        print(f"✘ 未设置 MCP Token(环境变量 {TOKEN_ENV})", file=sys.stderr)
        return 2
    if not config.store_code and not config.offline:
        print("✘ 需要 --store 指定门店编码", file=sys.stderr)
        return 2

    requirements = CateringRequirements(store_code=config.store_code,
                                        city=config.city)
    session = Session(config, progress=_emit)
    try:
        context = session.resolve_store(requirements)
        catalog = session.load_catalog(context, max_items=args.max_items)
    except MCPError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 3

    items = list(catalog)
    if args.role:
        items = [i for i in items if i.role.value == args.role]
    if args.sort == "price":
        items.sort(key=lambda i: (i.price or 10 ** 9, i.name))
    elif args.sort == "protein":
        items.sort(key=lambda i: (-i.protein_per_yuan(), i.price))
    else:
        items.sort(key=lambda i: (i.kcal if i.kcal is not None else 10 ** 9))

    stats = catalog.stats()
    print(f"门店 {stats['storeName'] or stats['storeCode']}  "
          f"共 {stats['total']} 个编码,{stats['priced']} 个有价格,"
          f"营养覆盖 {stats['nutritionRate'] * 100:.0f}%")
    print(f"{'编码':<14}{'餐品':<26}{'角色':<6}{'价格':>8}{'热量':>8}{'蛋白':>7}{'蛋白/元':>9}")
    print("─" * 80)
    for item in items[:args.limit]:
        kcal = f"{item.kcal:.0f}" if item.kcal is not None else "—"
        protein = f"{item.protein:.0f}" if item.protein is not None else "—"
        print(f"{item.code:<14}{item.name[:24]:<26}{item.role.label:<6}"
              f"{item.price_yuan:>8.2f}{kcal:>8}{protein:>7}{item.protein_per_yuan():>9.2f}")
    if len(items) > args.limit:
        print(f"... 另有 {len(items) - args.limit} 条,可用 --limit 调整")

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump({"context": session.trace_dict(), **catalog.to_dict()},
                      handle, ensure_ascii=False, indent=2)
        print(f"✔ 已写入 {args.json_path}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------- tools
def cmd_tools(args: argparse.Namespace, config: AppConfig) -> int:
    if not config.has_token and not config.offline:
        print(f"✘ 未设置 MCP Token(环境变量 {TOKEN_ENV})", file=sys.stderr)
        return 2
    session = Session(config)
    try:
        tools = session.client.list_tools()
    except MCPError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 3
    print(f"麦当劳 MCP Server 共暴露 {len(tools)} 个工具:")
    print("─" * 68)
    for index, tool in enumerate(sorted(tools, key=lambda t: t.get("name", "")), 1):
        required = (tool.get("inputSchema") or {}).get("required") or []
        marker = f"  必填:{','.join(required)}" if required else "  无必填参数"
        print(f"{index:>3}. {tool.get('name', ''):<26}{marker}")
    return 0


# ------------------------------------------------------------------- 比价
def cmd_compare(args: argparse.Namespace, config: AppConfig) -> int:
    """多门店比价:同一地址下对每家可配送门店各求解一次,按实付排序。"""
    from .compare import (
        DEFAULT_COMPARE_EFFORT,
        compare_stores,
        render_comparison,
        render_comparison_markdown,
    )

    if not config.has_token:
        print(f"✘ 比价需要麦当劳 MCP Token(环境变量 {TOKEN_ENV})", file=sys.stderr)
        return 2

    text = " ".join(args.text or [])
    overrides = _plan_overrides(args)
    overrides.pop("store_code", None)          # 门店由比价逻辑决定
    requirements = build_requirements(text, **overrides)

    session = Session(config, progress=_emit if config.verbose else None)
    try:
        comparison = compare_stores(
            session, requirements,
            max_stores=args.max_stores,
            effort=args.effort or DEFAULT_COMPARE_EFFORT,
            verify=not args.no_verify,
            progress=_emit,
        )
    except ValueError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 2
    except MCPError as exc:
        print(f"✘ 比价失败:{exc}", file=sys.stderr)
        return 3

    print(render_comparison(comparison, color=not args.no_color))

    best = comparison.best
    if best and best.plan:
        print(f"  ▶ 想用最省的那家下单:")
        print(f"    python -m mcdteam --store {best.store_code} order preview "
              f"\"{text or '需求'}\" --mode {args.mode}")
        print()

    if args.md_path:
        with open(args.md_path, "w", encoding="utf-8") as handle:
            handle.write(render_comparison_markdown(comparison))
        print(f"  ✔ Markdown 已写入 {args.md_path}", file=sys.stderr)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(comparison.to_dict(), handle, ensure_ascii=False,
                      indent=2, default=str)
        print(f"  ✔ JSON 已写入 {args.json_path}", file=sys.stderr)
    return 0 if best else 1


# ----------------------------------------------------------------- leaderboard
def cmd_scan(args: argparse.Namespace, config: AppConfig) -> int:
    from .secret_scan import main as scan_main
    return scan_main(["--path", args.path])


def cmd_leaderboard(args: argparse.Namespace, config: AppConfig) -> int:
    from .contest import fetch_leaderboard, ranking_updated_at, fetch_ranking_markdown
    try:
        markdown = fetch_ranking_markdown()
        rows = fetch_leaderboard()
    except Exception as exc:  # noqa: BLE001
        print(f"✘ 抓取失败:{exc}", file=sys.stderr)
        return 3
    updated = ranking_updated_at(markdown)
    if updated:
        print(f"官方排行榜更新时间:{updated}")
    print(f"{'名次':>4}  {'Star':>5}  {'项目':<40}{'Owner':<12}")
    print("─" * 68)
    for row in rows[:args.limit]:
        print(f"{row['rank']:>4}  {row['stars']:>5}  {row['title'][:38]:<40}{row['owner'][:10]:<12}")
    if len(rows) > args.limit:
        print(f"... 榜单共 {len(rows)} 个项目,可用 --limit 查看")
    if not rows:
        print("  (没有解析到 Star 数据,排行榜格式可能已变化)")
    return 0


# ---------------------------------------------------------------------- probe
def cmd_probe(args: argparse.Namespace, config: AppConfig) -> int:
    from .probe import run_probe
    if not config.has_token:
        print(f"✘ 未设置 MCP Token(环境变量 {TOKEN_ENV})", file=sys.stderr)
        return 2
    print("🔍 麦当劳 MCP 能力探针")
    print("─" * 68)
    report = run_probe(config, store_code=config.store_code)
    print("─" * 68)
    ok = sum(1 for item in report["results"] if item.get("ok"))
    print(f"  成功 {ok}/{len(report['results'])}   暴露工具 {report.get('toolCount')} 个")
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(f"  报告已写入 {args.out}", file=sys.stderr)
    return 0 if ok else 1


def cmd_agent_plan(args: argparse.Namespace, config: AppConfig) -> int:
    """把 :func:`agent_plan` 的结果打到 stdout;失败也返回 0 退出码。

    为什么失败也用 0:Agent 习惯把非零退出码当成"命令本身坏了"并放弃重试。
    这里的失败是**业务失败**(需求不全、Token 缺失),信封里已经带了
    ``status`` 与 ``error.hint``,让 Agent 继续读 JSON 比让它崩掉更有用。
    """
    envelope = agent_plan(args, config)
    payload = json.dumps(envelope, ensure_ascii=False, indent=2, default=str)
    if getattr(args, "json_path", None):
        try:
            with open(args.json_path, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except OSError as exc:  # pragma: no cover - 磁盘异常
            print(f"  ⚠ 无法写入 {args.json_path}:{exc}", file=sys.stderr)
    print(payload)
    return 0


# ----------------------------------------------------------------------- main
def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    config = AppConfig.load(
        token=args.token, store_code=args.store, city=args.city,
        offline=args.offline, verbose=args.verbose, fixture_dir=args.fixtures,
        address_id=args.address_id, be_code=args.be_code, endpoint=args.endpoint,
    )

    handlers = {
        "doctor": cmd_doctor,
        "plan": cmd_plan,
        "agent-plan": cmd_agent_plan,
        "order": cmd_order,
        "compare": cmd_compare,
        "menu": cmd_menu,
        "tools": cmd_tools,
        "probe": cmd_probe,
        "scan": cmd_scan,
        "leaderboard": cmd_leaderboard,
    }
    try:
        return handlers[args.command](args, config)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
