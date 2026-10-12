"""运行配置与令牌管理。

**安全红线**:MCP Token 只允许来自环境变量或命令行显式传参,
任何代码路径都不会把它写进日志、快照、报告或仓库文件。报告与错误信息
统一走 :func:`redact` 做脱敏。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

TOKEN_ENV = "MCD_MCP_TOKEN"
ENDPOINT_ENV = "MCD_MCP_ENDPOINT"
STORE_ENV = "MCD_STORE_CODE"
FIXTURE_ENV = "MCD_FIXTURES"
BE_CODE_ENV = "MCD_BE_CODE"
ADDRESS_ENV = "MCD_ADDRESS_ID"

#: 仓库内置的离线快照目录名
DEFAULT_FIXTURE_DIR = "fixtures"


def redact(text: Any, secrets: Optional[List[str]] = None) -> str:
    """把出现的令牌替换为掩码,用于任何可能被打印或落盘的字符串。"""
    result = str(text)
    candidates = list(secrets or [])
    env_token = os.environ.get(TOKEN_ENV)
    if env_token:
        candidates.append(env_token)
    for secret in candidates:
        if secret and len(secret) >= 8:
            result = result.replace(secret, "***REDACTED***")
    return result


def mask_token(token: str) -> str:
    """展示用掩码:``2U4m****D5Fz``。"""
    if not token:
        return "(未设置)"
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}****{token[-4:]}"


@dataclass
class AppConfig:
    """一次运行所需的全部外部配置。"""

    token: str = ""
    endpoint: str = "https://mcp.mcd.cn"
    store_code: str = ""
    store_name: str = ""
    be_code: str = ""
    address_id: str = ""
    city: str = ""
    fixture_dir: str = DEFAULT_FIXTURE_DIR
    offline: bool = False
    verbose: bool = False
    timeout: float = 30.0
    notes: List[str] = field(default_factory=list)

    @classmethod
    def load(cls, *, token: str = "", store_code: str = "", city: str = "",
             offline: bool = False, verbose: bool = False,
             fixture_dir: str = "", address_id: str = "",
             be_code: str = "", endpoint: str = "") -> "AppConfig":
        config = cls(
            token=(token or os.environ.get(TOKEN_ENV, "")).strip(),
            endpoint=(endpoint or os.environ.get(ENDPOINT_ENV, "") or "https://mcp.mcd.cn").strip(),
            store_code=(store_code or os.environ.get(STORE_ENV, "")).strip(),
            be_code=(be_code or os.environ.get(BE_CODE_ENV, "")).strip(),
            address_id=(address_id or os.environ.get(ADDRESS_ENV, "")).strip(),
            city=city.strip(),
            fixture_dir=(fixture_dir or os.environ.get(FIXTURE_ENV, "")
                         or _default_fixture_dir()),
            offline=offline,
            verbose=verbose,
        )
        return config

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    def resolve_fixture(self, name: str) -> str:
        return os.path.join(self.fixture_dir, name)

    def describe(self) -> Dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "token": mask_token(self.token),
            "storeCode": self.store_code or None,
            "city": self.city or None,
            "beCode": self.be_code or None,
            "offline": self.offline,
            "fixtureDir": self.fixture_dir,
        }


def _default_fixture_dir() -> str:
    """定位仓库内的 ``fixtures`` 目录(兼容从任意工作目录运行)。"""
    here = os.path.dirname(os.path.abspath(__file__))
    # src/mcdteam/config.py -> ../../fixtures
    candidate = os.path.abspath(os.path.join(here, "..", "..", DEFAULT_FIXTURE_DIR))
    if os.path.isdir(candidate):
        return candidate
    return DEFAULT_FIXTURE_DIR
