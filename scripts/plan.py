"""Agent 入口:一句话团餐需求 → 确定性方案 JSON。

这是 SKILL.md 里约定的主要调用方式。脚本只做一件事:
把需求交给引擎,**在 stdout 输出一个结构固定的 JSON 信封**,
Agent 直接读字段即可,不需要解析人类可读文本。

用法::

    python scripts/plan.py "14个人 预算480元 不吃辣 两个减脂 12点送到 要能分着吃的"
    python scripts/plan.py "10个人 预算300元" --store 1950564 --json plan.json
    python scripts/plan.py "8个人 预算200元" --offline --store 1950564

输出信封(成功)::

    {
      "status": "ok",
      "ok": true,
      "plan": { "payableYuan": ..., "items": [...], "warnings": [...], ... },
      "requirements": { ... },
      "context": { "calls": [...], "steps": [...] },
      "meta": { "verifiedByApi": true, "source": "...", "chartCard": "..." }
    }

输出信封(失败,退出码仍为 0,便于 Agent 继续处理)::

    {
      "status": "error",
      "ok": false,
      "error": { "type": "missing_token", "message": "...", "hint": "..." }
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: E402

_bootstrap.bootstrap()

from mcdteam.cli import _build_parser, agent_plan  # noqa: E402
from mcdteam.config import AppConfig  # noqa: E402


def build_agent_parser() -> argparse.ArgumentParser:
    """只暴露全局参数 + `agent-plan` 的参数,不带任何子命令。"""
    parser = _build_parser()
    for action in list(parser._actions):  # noqa: SLF001 - 需要剔除子命令动作
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            parser._remove_action(action)  # noqa: SLF001
            break
    parser.prog = "scripts/plan.py"
    parser.description = "麦麦团餐统筹官 · 生成团餐采购方案(输出 JSON 信封)"
    # 把 agent-plan 的参数直接提升为顶层参数
    for flags, kwargs in (
        (("text",), {"nargs": "*", "help": "自然语言需求"}),
        (("--headcount",), {"type": int}),
        (("--budget",), {"type": float}),
        (("--per-person",), {"type": float}),
        (("--mode",), {}),
        (("--at",), {"dest": "reserve_at"}),
        (("--avoid",), {"action": "append", "default": []}),
        (("--prefer",), {"action": "append", "default": []}),
        (("--max-kcal",), {"type": float}),
        (("--min-protein",), {"type": float}),
        (("--effort",), {"type": int, "default": 48}),
        (("--max-items",), {"type": int, "default": 0}),
        (("--no-verify",), {"action": "store_true"}),
        (("--json",), {"dest": "json_path", "help": "同时写入该文件"}),
    ):
        parser.add_argument(*flags, **kwargs)
    return parser


def main(argv=None) -> int:
    args = build_agent_parser().parse_args(argv)
    config = AppConfig.load(
        token=args.token, store_code=args.store, city=args.city,
        offline=args.offline, verbose=args.verbose, fixture_dir=args.fixtures,
        address_id=args.address_id, be_code=args.be_code, endpoint=args.endpoint,
    )
    envelope = agent_plan(args, config)
    payload = json.dumps(envelope, ensure_ascii=False, indent=2, default=str)
    if getattr(args, "json_path", None):
        try:
            with open(args.json_path, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except OSError as exc:
            print(f"⚠ 无法写入 {args.json_path}:{exc}", file=sys.stderr)
    print(payload)
    # 业务失败也用 0 退出码:Agent 应读 error.hint 决定下一步,而不是当作命令故障
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
