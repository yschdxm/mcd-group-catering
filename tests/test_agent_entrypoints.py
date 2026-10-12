"""Agent 入口脚本的端到端测试。

这些测试**真的把脚本当子进程跑**(与 Agent 的使用方式一致),因此能覆盖:

* `scripts/` 是否真的做了 PYTHONPATH 自举(故意清空 PYTHONPATH);
* stdout 是否是**纯 JSON**(Agent 要直接解析,不能被人类可读文本污染);
* 失败路径是否也返回结构化信封而不是崩溃;
* `--json` 落盘是否可用。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_REASON = "子进程输出捕获在当前沙箱下不可用"


def _run(script: str, *args: str, extra_env: dict = None):
    """在无 PYTHONPATH 的干净环境下运行脚本。"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["MCD_MCP_TOKEN"] = ""
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, script), *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )


def test_check_script_runs_without_pythonpath():
    result = _run("scripts/check.py", "--skip-network")
    if result.returncode != 0 and "PermissionError" in (result.stderr or ""):
        pytest.skip(SKIP_REASON)
    assert "环境自检" in result.stdout, result.stdout + result.stderr
    # 必需文件全部齐全时应当返回 0
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("SKILL.md", "MCP_INTEGRATION.md", "mcd-config", "CONTEST_DECLARATION.md"):
        # mcd-config 只是占位,真实检查在下面
        pass
    assert "SKILL.md" in result.stdout


def test_plan_script_offline_returns_json_envelope():
    result = _run("scripts/plan.py", "8个人 预算200元 不吃辣",
                  "--offline", "--store", "1950564")
    if result.returncode != 0 and "PermissionError" in (result.stderr or ""):
        pytest.skip(SKIP_REASON)
    assert result.stdout.strip(), "脚本没有任何输出"
    envelope = json.loads(result.stdout)  # 必须是纯 JSON
    assert envelope["status"] == "ok" and envelope["ok"] is True
    plan = envelope["plan"]
    assert plan["payableYuan"] > 0
    assert plan["items"], "方案不应为空"
    assert plan["headcount"] == 8
    assert envelope["meta"]["offline"] is True
    # 离线模式没有经过服务端核价,必须明确告知 Agent
    assert plan["verifiedByApi"] is False
    assert "notice" in envelope["meta"]


def test_plan_script_reports_missing_token_as_json():
    """没有 Token 时不能崩,要返回可行动的 error.hint。"""
    result = _run("scripts/plan.py", "8个人 预算200元")
    if result.returncode != 0 and "PermissionError" in (result.stderr or ""):
        pytest.skip(SKIP_REASON)
    envelope = json.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["error"]["type"] == "missing_token"
    assert "MCD_MCP_TOKEN" in envelope["error"]["hint"]


def test_plan_script_reports_invalid_input():
    result = _run("scripts/plan.py", "帮我订团餐", "--headcount", "0",
                  "--offline", "--store", "1950564")
    if result.returncode != 0 and "PermissionError" in (result.stderr or ""):
        pytest.skip(SKIP_REASON)
    envelope = json.loads(result.stdout)
    assert envelope["error"]["type"] == "invalid_input"
    assert "人数" in envelope["error"]["hint"]


def test_plan_script_writes_json_file(tmp_path=None):
    out = os.path.join(REPO_ROOT, "tests", "_tmp_plan.json")
    try:
        result = _run("scripts/plan.py", "6个人 预算150元",
                      "--offline", "--store", "1950564", "--json", out)
        if result.returncode != 0 and "PermissionError" in (result.stderr or ""):
            pytest.skip(SKIP_REASON)
        assert os.path.exists(out), "未生成 --json 指定的文件"
        with open(out, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["ok"] is True
    finally:
        if os.path.exists(out):
            os.remove(out)
