"""Agent 入口:环境自检。

Agent 在第一次使用、或遇到任何异常时,应先跑这个脚本确认环境是否就绪。

用法::

    python scripts/check.py
    python scripts/check.py --skip-network    # 只做本地检查

退出码:0 = 就绪;1 = 存在问题(输出中会给出手工修复建议)。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: E402

_bootstrap.bootstrap()

from mcdteam import __version__  # noqa: E402
from mcdteam.config import TOKEN_ENV, AppConfig, mask_token  # noqa: E402
from mcdteam.mcp.errors import MCPAuthError, MCPError  # noqa: E402

REQUIRED_FILES = [
    ("README.md", "项目介绍"),
    ("CONTEST_DECLARATION.md", "参赛声明"),
    ("MCP_INTEGRATION.md", "MCP 集成说明"),
    ("mcp-config.example.json", "脱敏 MCP 配置"),
    ("SKILL.md", "Agent Skill 定义"),
]

FIXTURES = [
    ("nutrition_raw.txt", "营养数据快照"),
    ("query-meals.1950564.json", "门店菜单快照"),
    ("price-map.store1950564.json", "门店价格快照"),
]


def _pad(text: str, width: int) -> str:
    """按终端显示宽度补空格(中文占 2 列),避免输出表格错位。"""
    shown = sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)
    return text + " " * max(0, width - shown)


def _line(ok: bool, label: str, detail: str = "") -> str:
    mark = "✔" if ok else "✘"
    tail = f"   {detail}" if detail else ""
    return f"     {mark} {_pad(label, 18)}{tail}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/check.py",
                                     description="麦麦团餐统筹官 · 环境自检")
    parser.add_argument("--store", default="", help="顺带检查该门店是否可取菜单")
    parser.add_argument("--skip-network", action="store_true", help="只做本地检查")
    args = parser.parse_args(argv)

    root = _bootstrap.repo_root()
    problems = []

    print("🍟 麦麦团餐统筹官 · 环境自检")
    print("=" * 62)
    print(f"  仓库根目录   {root}")
    print(f"  Python       {sys.version.split()[0]}  ({sys.executable})")
    print(f"  程序版本     {__version__}")

    config = AppConfig.load()
    print(f"  MCP Token    {mask_token(config.token)}")
    print(f"  接入地址     {config.endpoint}")
    print(f"  快照目录     {config.fixture_dir}")
    print("-" * 62)

    print("  📄 比赛必需文件")
    for name, label in REQUIRED_FILES:
        path = os.path.join(root, name)
        ok = os.path.exists(path)
        print(_line(ok, label, name if ok else f"{name}  (缺失)"))
        if not ok:
            problems.append(f"缺少必需文件 {name}")

    print("  📦 离线快照")
    for name, label in FIXTURES:
        path = os.path.join(config.fixture_dir, name)
        ok = os.path.exists(path)
        size = f"{os.path.getsize(path) / 1024:.1f} KB" if ok else "缺失(仅影响 --offline)"
        print(_line(ok, label, size))
    try:
        import glob
        store_codes = sorted({
            os.path.basename(path).split(".")[1]
            for path in glob.glob(os.path.join(config.fixture_dir, "query-meals.*.json"))
            if len(os.path.basename(path).split(".")) >= 3
        })
        if store_codes:
            print(f"     ℹ 已抓取门店快照 {len(store_codes)} 家:"
                  f"{', '.join(store_codes)}")
    except OSError:
        pass
    # 快照缺失不阻塞在线使用,只提示
    if not os.path.exists(os.path.join(config.fixture_dir, "nutrition_raw.txt")):
        print("     ⚠ 营养快照缺失:营养参考会为空,但不影响价格与清单")

    print("  🧩 依赖检查")
    try:
        import mcdteam  # noqa: F401
        print("     ✔ mcdteam 可导入(零第三方依赖)")
    except Exception as exc:  # noqa: BLE001
        print(f"     ✘ mcdteam 导入失败:{exc}")
        problems.append("mcdteam 无法导入")

    if args.skip_network:
        print("=" * 62)
        return _finish(problems, skipped=True)

    if not config.has_token:
        print("  🌐 网络检查")
        print(f"     ⚠ 未设置 {TOKEN_ENV},只能使用 --offline 模式")
        print("       申请 Token:https://open.mcd.cn/mcp(登录 → 控制台 → 激活)")
        print("       PowerShell:  $env:MCD_MCP_TOKEN=\"你的Token\"")
        print("       Bash:        export MCD_MCP_TOKEN=\"你的Token\"")
        print("=" * 62)
        return _finish(problems, skipped=True)

    print("  🌐 网络检查")
    from mcdteam.mcp.client import MCPClient  # noqa: E402

    client = MCPClient(config.token, endpoint=config.endpoint,
                       snapshot_dir=config.fixture_dir, allow_snapshot=False)
    try:
        info = client.initialize()
        server = info.get("serverInfo", {})
        print(f"     ✔ 握手成功:{server.get('name')} v{server.get('version')}")
        tools = client.list_tools()
        print(f"     ✔ 工具列表:{len(tools)} 个")
    except MCPAuthError as exc:
        print(f"     ✘ 鉴权失败:{exc}")
        problems.append("MCP Token 无效或已过期")
    except MCPError as exc:
        print(f"     ✘ 连接失败:{exc}")
        problems.append("无法连接 MCP Server")

    if args.store:
        try:
            payload = client.query_meals(args.store)
            codes = {str(m.get("code")) for g in payload.get("categories", []) or []
                     for m in g.get("meals", []) or []}
            print(f"     ✔ 门店 {args.store}:{len(codes)} 个可售编码")
        except MCPError as exc:
            print(f"     ✘ 门店 {args.store}:{exc}")
            problems.append(f"门店 {args.store} 无法取菜单")

    print("=" * 62)
    return _finish(problems, skipped=False)


def _finish(problems, *, skipped: bool) -> int:
    if problems:
        print("  结果:存在问题,需先修复")
        for item in problems:
            print(f"     · {item}")
        return 1
    if skipped:
        print("  结果:本地检查通过(未做网络检查)")
    else:
        print("  结果:环境就绪,可以执行 scripts/plan.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
