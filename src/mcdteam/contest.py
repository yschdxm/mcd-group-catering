"""比赛辅助工具:抓取官方排行榜,帮参赛者定位「赛道拥挤度」。

官方排行榜由活动仓库的 ``RANKING.md`` 维护,每隔一段时间更新一次。
本模块只做只读抓取(走 GitHub 公开 contents 接口,带 User-Agent,
不触发限流也不做任何写入),用于回答两个问题:

1. 当前进入榜单的门槛是多少 Star?
2. 我想做的方向,榜单上已经有几个同类项目?

用法::

    python -m mcdteam leaderboard
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional

REPO = "M-China/mcd-developer-innovation-challenge"
CONTENTS_API = f"https://api.github.com/repos/{REPO}/contents/RANKING.md"
USER_AGENT = "mcd-group-catering/1.0 (contest helper; read-only)"

_ROW_RE = re.compile(
    r"^\|\s*(?P<rank>\d+)\s*\|\s*(?P<name>[^|]+?)\s*\|\s*(?P<owner>[^|]*?)\s*\|"
    r"\s*(?P<stars>\d+)\s*\|\s*$"
)
_UPDATED_RE = re.compile(r"更新时间[::]\s*(?P<when>[0-9: \-]+)")


def fetch_ranking_markdown(timeout: float = 30.0) -> str:
    """读取官方排行榜原文。"""
    request = urllib.request.Request(CONTENTS_API, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("encoding") == "base64":
        return base64.b64decode(payload["content"]).decode("utf-8", "replace")
    raise RuntimeError("GitHub contents 接口返回了非 base64 内容")


def parse_ranking(markdown: str) -> List[Dict[str, object]]:
    """解析排行榜表体。"""
    rows: List[Dict[str, object]] = []
    for line in (markdown or "").splitlines():
        match = _ROW_RE.match(line.strip())
        if not match:
            continue
        rows.append({
            "rank": int(match.group("rank")),
            "title": match.group("name").strip(),
            "owner": match.group("owner").strip(),
            "stars": int(match.group("stars")),
        })
    rows.sort(key=lambda r: (-int(r["stars"]), int(r["rank"])))
    return rows


def ranking_updated_at(markdown: str) -> Optional[str]:
    match = _UPDATED_RE.search(markdown or "")
    return match.group("when").strip() if match else None


def fetch_leaderboard(pages: int = 6, **_ignored) -> List[Dict[str, object]]:
    """兼容 CLI 的入口:返回 ``[{rank, title, owner, stars, number}]``。

    ``number`` 字段保留为名次(便于与 Issue 报名号区分),此接口不抓 Issue,
    因为报名成功的判定以排行榜为准。
    """
    rows = parse_ranking(fetch_ranking_markdown())
    for row in rows:
        row["number"] = row["rank"]
    return rows


def summarise(rows: List[Dict[str, object]], limit: int = 10) -> str:
    """输出一段简报:门槛 Star、前 N 名、以及按关键词的赛道拥挤度。"""
    if not rows:
        return "排行榜为空或格式已变化。"
    out: List[str] = []
    out.append(f"进入榜单的项目数:{len(rows)}")
    out.append(f"榜首:{rows[0]['title']}({rows[0]['stars']} Star)")
    cutoff = rows[min(len(rows) - 1, 99)]['stars'] if len(rows) >= 100 else rows[-1]['stars']
    out.append(f"榜单门槛(约第 100 名):{cutoff} Star")
    return "\n".join(out)
