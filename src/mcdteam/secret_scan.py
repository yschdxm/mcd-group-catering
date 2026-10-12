"""密钥泄露扫描:提交前确认仓库里没有任何真实凭据。

为什么需要它:麦当劳 MCP Token 一旦进入公开仓库,任何人拿到都能以你的身份
调用点餐、积分兑换等接口。人工检查容易漏,所以做成脚本 + 可选的 pre-commit。

用法::

    python -m mcdteam.secret_scan           # 扫描仓库根目录
    python -m mcdteam.secret_scan --path .  # 指定路径

退出码:0 = 干净;1 = 发现疑似凭据。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Dict, Iterable, List, Optional, Sequence

#: 需要扫描的文本后缀
SCAN_EXTENSIONS = {
    ".py", ".md", ".json", ".txt", ".toml", ".cfg", ".ini", ".yml", ".yaml",
    ".js", ".ts", ".sh", ".ps1", ".env", ".example", ".gitignore",
}

#: 跳过的目录
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv",
             "node_modules", "dist", "build", ".mypy_cache"}

#: 允许出现的长随机串白名单(示例占位符、公开的 SDK 公钥等)
ALLOWED_PLACEHOLDERS = {
    "your_mcd_mcp_token_here", "YOUR_MCP_TOKEN", "MCD_MCP_TOKEN",
    "sk-xxxxxxxxxxxxxxxx", "test-token", "dummy", "example",
}

#: 规则:名称 + 正则 + 说明
RULES = [
    ("Bearer 后跟长串",
     re.compile(r"Bearer\s+([A-Za-z0-9_\-\.]{16,})"),
     "疑似在代码/文档里硬编码了 Bearer Token"),
    ("疑似 MCP Token 字面量",
     re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9]{28,34})(?![A-Za-z0-9_])"),
     "疑似硬编码的长随机凭据"),
    ("赋值式密钥",
     re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key)\s*[=:]\s*[\"']?([A-Za-z0-9_\-\.]{16,})"),
     "疑似把密钥写进了赋值语句"),
    ("私钥文件内容",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
     "疑似私钥"),
    ("GitHub Token",
     re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})\b"),
     "疑似 GitHub Token"),
]

#: 明显不是密钥的通用词(避免误报)
FALSE_POSITIVE_HINTS = (
    "streamablehttp", "application/json", "text/event-stream", "utf-8",
    "sha256", "md5", "sha1", "www.mcdonalds", "mcp.mcd.cn", "github.com",
    "open.mcd.cn", "creativecommons", "text/plain", "charset",
)


def iter_files(root: str) -> Iterable[str]:
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            _, ext = os.path.splitext(name)
            if ext.lower() in SCAN_EXTENSIONS or name in {".env.example", ".gitignore"}:
                yield os.path.join(current, name)


def scan_file(path: str, secrets: Sequence[str] = ()) -> List[Dict[str, object]]:
    findings: List[Dict[str, object]] = []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return findings

    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") and "Bearer" not in stripped:
            # 注释里的说明文字仍可能含占位符,但纯注释行跳过以减少噪音
            if not any(rule[1].search(line) for rule in RULES):
                continue
        for secret in secrets:
            if secret and secret in line:
                findings.append({
                    "file": path, "line": number, "rule": "环境变量中的真实 Token",
                    "detail": "文件内容与当前环境变量里的 Token 一致", "snippet": _mask(line),
                })
        for name, pattern, detail in RULES:
            for match in pattern.finditer(line):
                candidate = _pick_candidate(match)
                if not candidate or _is_allowed(candidate):
                    continue
                if _looks_like_false_positive(line, candidate):
                    continue
                findings.append({
                    "file": path, "line": number, "rule": name, "detail": detail,
                    "snippet": line.strip()[:120],
                })
    return findings


def _pick_candidate(match: "re.Match") -> str:
    if match.groups():
        for group in reversed(match.groups()):
            if group:
                return group
    return match.group(0)


def _is_allowed(candidate: str) -> bool:
    lowered = candidate.lower()
    if lowered in {p.lower() for p in ALLOWED_PLACEHOLDERS}:
        return True
    if any(hint in lowered for hint in FALSE_POSITIVE_HINTS):
        return True
    # 全数字(时间戳、编码、价格)不算凭据
    if candidate.isdigit():
        return True
    # 有分隔符的长串更可能是 URL / 路径 / 类名
    if candidate.count("-") + candidate.count("_") >= 3:
        return True
    return False


def _looks_like_false_positive(line: str, candidate: str) -> bool:
    lowered_line = line.lower()
    if any(hint in lowered_line for hint in ("placeholder", "占位", "示例", "your_",
                                             "xxx", "****", "redacted")):
        return True
    # 纯十六进制且长度 <= 32,多为 traceId / 摘要
    if re.fullmatch(r"[0-9a-fA-F]{8,32}", candidate):
        return True
    return False


def _mask(text: str) -> str:
    text = text.strip()
    if len(text) <= 24:
        return text
    return f"{text[:12]}…{text[-6:]}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mcdteam.secret_scan",
                                     description="提交前的密钥泄露扫描")
    parser.add_argument("--path", default=".", help="扫描根目录,默认当前目录")
    parser.add_argument("--token", default="", help="额外要检查的凭据(通常用环境变量)")
    args = parser.parse_args(argv)

    secrets = [s for s in [args.token, os.environ.get("MCD_MCP_TOKEN", "")] if s]

    print("🔐 密钥泄露扫描")
    print("─" * 68)
    findings: List[Dict[str, object]] = []
    scanned = 0
    for path in iter_files(os.path.abspath(args.path)):
        scanned += 1
        findings.extend(scan_file(path, secrets))

    if not findings:
        print(f"  ✔ 已扫描 {scanned} 个文件,未发现硬编码凭据")
        return 0

    print(f"  ✘ 在 {scanned} 个文件中发现 {len(findings)} 处疑似凭据:")
    for item in findings:
        relative = os.path.relpath(str(item["file"]), os.path.abspath(args.path))
        print(f"     {relative}:{item['line']}  [{item['rule']}] {item['detail']}")
        print(f"        {item['snippet']}")
    print("─" * 68)
    print("  处理建议:把凭据移到环境变量,并用 redact() 做输出脱敏后重新提交。")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
