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
