"""Skill 形态合规性测试。

本次比赛的作品形态是「Agent Skill」,因此除了代码逻辑,还必须保证:

1. 根目录存在 `SKILL.md`,且 frontmatter 符合通用 Agent Skill 规范
   (`name` 小写短横线、`description` 非空且有长度上限);
2. `description` 里包含足够多的高频触发词,Agent 才能在正确时机唤起本 Skill;
3. `scripts/` 下的 Agent 入口脚本存在、语法可解析、并带自举能力
   (不要求使用者预先设置 PYTHONPATH);
4. 比赛要求的必需文件齐全。
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

SKILL_PATH = os.path.join(REPO_ROOT, "SKILL.md")

REQUIRED_FILES = [
    "README.md",
    "CONTEST_DECLARATION.md",
    "MCP_INTEGRATION.md",
    "mcp-config.example.json",
    "SKILL.md",
]

#: Agent 需要能通过这些词判断"该用这个 Skill 了"
TRIGGER_WORDS = ["团餐", "预算", "忌口", "不吃辣", "素食", "加班餐", "下午茶"]


def _skill_text() -> str:
    with open(SKILL_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _frontmatter(text: str) -> dict:
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not match:
        return {}
    fields = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def test_skill_file_exists():
    assert os.path.exists(SKILL_PATH), "缺少 SKILL.md —— 本比赛的作品形态就是 Skill"


def test_skill_has_valid_frontmatter():
    fields = _frontmatter(_skill_text())
    assert "name" in fields and "description" in fields, "SKILL.md 缺少 name / description"


def test_skill_name_is_slug():
    name = _frontmatter(_skill_text())["name"]
    assert re.fullmatch(r"[a-z0-9-]+", name), f"name 必须是 [a-z0-9-]:{name!r}"
    assert 1 <= len(name) <= 64, f"name 长度应在 1~64:{len(name)}"
    # 目录名与 name 保持一致,便于按目录安装
    assert name == os.path.basename(REPO_ROOT), "SKILL.md 的 name 应与仓库/目录名一致"


def test_skill_description_length_and_content():
    description = _frontmatter(_skill_text())["description"]
    assert 1 <= len(description) <= 1024, f"description 长度应在 1~1024:{len(description)}"
    assert "<" not in description and ">" not in description, "description 不应包含尖括号"


@pytest.mark.parametrize("word", TRIGGER_WORDS)
def test_skill_description_mentions_triggers(word):
    """description 决定 Agent 何时唤起本 Skill,必须覆盖高频场景词。"""
    description = _frontmatter(_skill_text())["description"]
    assert word in description, f"description 缺少触发词:{word}"


def test_skill_documents_required_sections():
    text = _skill_text()
    for section in ["## 角色", "## 铁律", "## 工作流", "## 参考文件", "## 边界"]:
        assert section in text, f"SKILL.md 缺少必备小节:{section}"


def test_skill_states_no_fabrication_rules():
    """铁律必须包含"不得编造价格"与"不自动下单",这是本 Skill 的安全底线。"""
    text = _skill_text()
    assert "绝不编造价格" in text
    assert "不自动下单" in text
    assert "不构成医疗或营养建议" in text


def test_skill_references_exist():
    """SKILL.md 里引用的参考文件必须真实存在,否则 Agent 会读空。"""
    text = _skill_text()
    for rel in re.findall(r"references/[A-Za-z0-9_\-]+\.md", text):
        assert os.path.exists(os.path.join(REPO_ROOT, rel)), f"引用的文件不存在:{rel}"


def test_skill_documents_script_entrypoints():
    text = _skill_text()
    for script in ["scripts/check.py", "scripts/plan.py"]:
        assert script in text, f"SKILL.md 未说明入口脚本:{script}"
        assert os.path.exists(os.path.join(REPO_ROOT, script)), f"入口脚本不存在:{script}"


def test_required_files_present():
    for name in REQUIRED_FILES:
        assert os.path.exists(os.path.join(REPO_ROOT, name)), f"缺少比赛必需文件:{name}"


def test_contest_declaration_untouched():
    """参赛声明内容不可改动,至少校验关键声明段落仍在。"""
    with open(os.path.join(REPO_ROOT, "CONTEST_DECLARATION.md"), "r", encoding="utf-8") as handle:
        text = handle.read()
    for heading in ["## 原创性声明", "## 合规声明", "## 信息安全声明",
                    "## 使用提示", "## 规则遵守声明"]:
        assert heading in text, f"参赛声明缺少小节:{heading}"


def test_mcp_config_example_uses_placeholder_only():
    """脱敏配置里绝对不能出现真实 Token。"""
    with open(os.path.join(REPO_ROOT, "mcp-config.example.json"), "r",
              encoding="utf-8") as handle:
        text = handle.read()
    payload = json.loads(text)
    header = payload["mcpServers"]["mcd-mcp"]["headers"]["Authorization"]
    assert "${" in header and "}" in header, "必须使用环境变量占位符"
    assert not re.search(r"Bearer\s+[A-Za-z0-9]{20,}", text), "疑似写入了真实 Token"


@pytest.mark.parametrize("script", ["scripts/plan.py", "scripts/check.py",
                                    "scripts/_bootstrap.py"])
def test_script_syntax_and_bootstrap(script):
    """脚本必须语法正确,并且在使用 mcdteam 之前先做 sys.path 自举。"""
    path = os.path.join(REPO_ROOT, script)
    assert os.path.exists(path), f"脚本不存在:{script}"
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    ast.parse(source)  # 语法错误会直接抛异常
    if script != "scripts/_bootstrap.py":
        assert "_bootstrap" in source, f"{script} 未做 PYTHONPATH 自举"
        assert "bootstrap()" in source, f"{script} 未调用 bootstrap()"
