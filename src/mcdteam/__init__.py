"""麦麦团餐统筹官 / MCD Group Catering Orchestrator.

基于麦当劳中国 MCP Server(https://mcp.mcd.cn) 的企业团餐批量点餐统筹引擎。

模块划分::

    mcdteam.mcp       MCP Streamable HTTP 客户端与工具错误模型
    mcdteam.data      菜单索引 / 营养解析 / 优惠券归一化 / 本店价格快照
    mcdteam.solve     团餐约束求解器(场合解析、预算搜索、券叠加比价)
    mcdteam.report    终端卡片与 Markdown / JSON 报告输出
    mcdteam.cli       命令行入口(plan / doctor / menu / inspect / leaderboard)
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
