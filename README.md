# 🍟 麦麦团餐统筹官 · MCD Group Catering Orchestrator

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-96%20passed-brightgreen)](tests/)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Agent Skill](https://img.shields.io/badge/Agent%20Skill-SKILL.md-ffc72c)](SKILL.md)
[![MCP](https://img.shields.io/badge/MCP-mcd.mcd.cn-ffc72c)](MCP_INTEGRATION.md)

> 一句话需求 → 一份**可下单、不超预算、不踩忌口**的企业团餐采购清单。

「周五中午 14 个人,预算 480,两人减脂,一个不吃辣,一个不吃牛肉,再来点能分着吃的」
—— 这句话丢给本项目,30~60 秒后你会拿到一份带优惠券抵扣、营养合计、
分餐配比和实价核验的采购提案。

本项目是一个 **Agent Skill**(见 [`SKILL.md`](SKILL.md)):把它交给任意支持
Skill 的 AI Agent,用户直接说人话即可;也可以在命令行单独使用。
底层基于 [麦当劳中国 MCP Server](https://github.com/M-China/mcd-mcp-server) 开发,
是**麦当劳程序员节创意开发大赛**参赛作品。

```text
🍟 麦麦团餐统筹官 · 方案已生成
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  门店 1950564
  人数 8 人   ·   场合 麦乐送   ·   时段 lunch
  期望时间 2026-10-13 12:00
────────────────────────────────────────────────────────────────────
  📋 采购清单
  餐品                    角色          单价   数量        小计
  ····································································
  麦香鸡                主食     18.00    1     18.00
  吉士汉堡包            主食     16.50    2     33.00
  汉堡包                主食     15.00    5     75.00
  可口可乐中杯          饮料     11.00    3     33.00
  无糖可口可乐中杯      饮料     11.00    3     33.00
  苹果片                小食      7.50    2     15.00
  ····································································
  合计                                 16    207.00
────────────────────────────────────────────────────────────────────
  💰 结算
  商品原价                 ¥207.00
  优惠抵扣                   -5.20
  应付总额                 ¥219.00
  人均                      ¥27.38
  预算余量                   ¥1.00
  (金额已由麦当劳 calculate-price 接口实时回算)
  预算使用率 100%
────────────────────────────────────────────────────────────────────
  👥 每人怎么分
  主食 1 份/人  ·  饮料 每 1.3 人 1 份  ·  小食 每 4.0 人 1 份
```

---

## 目录

- [作为 Agent Skill 使用](#作为-agent-skill-使用)
- [为什么做这个](#为什么做这个)
- [快速开始](#快速开始)
- [使用示例](#使用示例)
- [命令参考](#命令参考)
- [它是怎么算的](#它是怎么算的)
- [项目结构](#项目结构)
- [测试](#测试)
- [安全与隐私](#安全与隐私)
- [常见问题](#常见问题)
- [推广与拉票素材](docs/PROMOTION.md)

---

## 作为 Agent Skill 使用

### 这是什么形态

本项目遵循通用 **Agent Skill** 约定:根目录的 [`SKILL.md`](SKILL.md) 是一份
「角色 + 铁律 + 工作流 + 脚本契约」的说明文档,由 AI Agent 读取后执行;
真正需要确定性计算的部分(取价、约束求解、核价收敛)交给 `scripts/` 下的脚本。

```text
用户说人话
   │
   ▼
AI Agent(Claude / WorkBuddy / Cursor / 任何支持 Skill 的宿主)
   ├─ 读 SKILL.md:知道什么时候该用、必须遵守哪些铁律、按什么流程走
   ├─ 直接调用麦当劳 MCP 工具(参数链规则写在 SKILL.md 里)
   └─ 调用 scripts/plan.py 拿确定性结果,再组织成人话回给用户
```

**为什么这样分工**:价格与配额必须可复现、可核对,不能交给模型估算。
所以 SKILL.md 的第一条铁律就是「绝不编造价格,任何金额必须来自工具返回」。

### 安装到 Agent 宿主

不同宿主的安装方式不同,但共同点是「把**整个仓库目录**放进去」——
因为 `SKILL.md` 里引用的 `scripts/` 与 `references/` 必须一起可用。

| 宿主 | 放哪里 |
|---|---|
| Claude Code / Claude Desktop | 项目目录放 `.claude/skills/mcd-group-catering/`,或作为 Skill 目录导入 |
| Cursor / VS Code + Copilot | 放进工作区,Agent 会自动读取根目录 `SKILL.md` |
| WorkBuddy | 作为自定义 Skill 导入本仓库目录 |
| 其他宿主 | 只要宿主能读 `SKILL.md` 并能执行 shell 命令即可 |

Skill 的 `name` 是 `mcd-group-catering`,与目录名一致,便于按目录安装。

### 首次使用:让 Agent 先自检

在对话里说:

> 用 mcd-group-catering 这个 Skill,先跑一下环境自检

Agent 会执行 `python scripts/check.py`,一次性确认:
必需文件是否齐全、MCP Token 是否就绪、能否连上 MCP Server、离线快照是否完整。

### Agent 侧的调用契约

主入口是 `scripts/plan.py`,**stdout 只输出一个 JSON 信封**,
不需要预先设置 `PYTHONPATH`(脚本内部自举):

```bash
python scripts/plan.py "14个人 预算480元 不吃辣 两个减脂 12点送到" --json plan.json
```

成功时:

```json
{
  "status": "ok",
  "ok": true,
  "plan": {
    "payableYuan": 219.0, "perPersonYuan": 27.38, "underBudget": true,
    "verifiedByApi": true, "roleCounts": {"主食": 8, "饮料": 6},
    "items": [{"productName": "汉堡包", "roleLabel": "主食",
               "quantity": 5, "unitPriceYuan": 15.0}],
    "nutrition": {"perPerson": {"kcal": 283, "protein": 14}},
    "warnings": [], "relaxations": [], "couponsApplied": []
  },
  "requirements": {"headcount": 14, "budgetYuan": 480.0},
  "meta": {"verifiedByApi": true, "source": "麦当劳 MCP 实时接口",
           "chartCard": "可直接发到群里的一段文本"}
}
```

失败时也返回结构化信封(**退出码仍为 0**),并附带可行动的 `hint`:

```json
{
  "status": "error",
  "ok": false,
  "error": {
    "type": "missing_token",
    "message": "未设置麦当劳 MCP Token(环境变量 MCD_MCP_TOKEN)",
    "hint": "到 https://open.mcd.cn/mcp 登录后激活 Token…或先用 --offline"
  }
}
```

### Agent 的参考文件

`SKILL.md` 会按需引导 Agent 读取这三份文件,避免它凭空推断:

| 文件 | 内容 |
|---|---|
| [`references/toolchain.md`](references/toolchain.md) | 工具参数链规则、`code→productCode` 映射、按场景的调用顺序、九个实测坑 |
| [`references/planning-rules.md`](references/planning-rules.md) | 配比配额公式、目标函数权重、约束让位顺序、预算收敛规则 |
| [`references/troubleshooting.md`](references/troubleshooting.md) | `缺少参数`、空方案、Token 失效、营养偏低等问题的排查步骤 |

### 也可以完全不用 Agent(纯 CLI)

如果你只想在终端里用,直接看下面的[快速开始](#快速开始)。
两条路径共用同一个引擎,输出一致。

### 下单(默认不执行)

麦当劳 MCP **没有沙箱/测试环境** —— `create-order` 的 schema 中不存在任何
dry-run 或测试标志,调用成功即产生**真实待支付订单**。因此本项目把下单关在三道闸门后面:

| 闸门 | 怎么开 |
|---|---|
| ① 用户明确确认 | 用户说出"下单""确认"之类的话 |
| ② 命令行 `--pay` | `order pay ... --pay` |
| ③ 环境变量 | `MCD_ALLOW_REAL_ORDER=1` |

**先预览(安全,零副作用)**:

```bash
python -m mcdteam --store 1950564 order preview "6个人 预算150元 不吃辣 到店取" --mode pickup
```

```
🧾 下单请求装配
门店        1950564
取餐方式    到店自取(orderType=1, beType=1)
取餐方式码  eat-in
商品件数    13 件
服务端报价  ¥147.00
方案金额    ¥147.00

  可选取餐方式:eat-in(堂食)、take-in-store(外带)
  · dry-run:未调用 create-order
⏸ 未下单(dry-run 或安全闸门未放行)
```

**真正下单**:

```powershell
$env:MCD_ALLOW_REAL_ORDER = "1"      # 必须由使用者本人设置
python -m mcdteam --store 1950564 order pay "6个人 预算150元 不吃辣 到店取" --mode pickup --pay
```

提交前会**重新核价**;若与服务端报价相差超过 ¥1.00,默认拒绝提交并把差异告诉你,
需要显式 `--allow-amount-mismatch` 才继续。

下单后可用 `order status --order-id <订单号>` 查询、
`order cancel --order-id <订单号> --reason-code 1` 取消。

> Agent 侧被明确禁止:不得替用户设置 `MCD_ALLOW_REAL_ORDER`,
> 也不得为了"试试看"而下单(见 `SKILL.md` 铁律第 10 条)。

---

## 为什么做这个

企业团餐 / 部门加班餐 / 会议茶歇的统筹者(行政、HR、助理)每次都要面对同一组难题:

| 难题 | 人工现状 | 本项目的处理 |
|---|---|---|
| 预算不能超 | 靠心算,经常超 | 约束求解保证预算内;**门店实价超预算时自动削减并重新核价** |
| 忌口不能漏 | 容易漏掉某个人的禁忌 | 硬约束过滤;若因此无主食可用,**按优先级让位并明确告警** |
| 配比要合理 | 小食买一堆、主食不够吃 | 主食配额 = 人数(硬约束),小食/饮料按人数比例推算 |
| 券要用上 | 结账时才发现有券 | 自动匹配门店券 + 卡包券,**只接受真正降低应付的券** |
| 营养要透明 | 没有数据 | 挂载官方 160 条营养数据,给出整单/人均热量与蛋白质 |
| 耗时 | 人工比价 20~40 分钟 | 一次调用完成,典型 30~60 秒 |

### 和「单人选餐」类工具的差异

榜单上多数作品解决的是**单人在某一家店的决策**(选什么、省多少、热量多少)。
本项目解决的是**多人多约束的批量统筹**:

- 决策单元是「一份 14 人的采购清单」,不是「一个人的一顿饭」;
- 约束来源是**多人禁忌的并集**(有人不吃辣 + 有人素食 + 有人乳糖不耐);
- 使用团餐专属链路(`beType=6`、`query-promotions`、`query-meal-assistance`);
- 输出物是**给行政审批的提案**(Markdown)+ **给同事群看的短卡片**。

---

## 快速开始

### 方式一:零配置离线演示(30 秒,推荐先跑这个)

仓库内置了一份**真实抓取**的门店快照(北京建外 SOHO A 座,117 个餐品 + 160 条营养数据),
不需要 Token、不联网即可完整跑通求解链路:

```bash
git clone https://github.com/yschdxm/mcd-group-catering.git
cd mcd-group-catering
export PYTHONPATH=src        # Windows: $env:PYTHONPATH="src"

python -m mcdteam --offline --store 1950564 \
  plan "14个人 预算480元 不吃辣 两个减脂 12点送到 要能分着吃的"
```

### 方式二:接入真实 MCP(推荐用于实际下单)

```bash
export MCD_MCP_TOKEN="你的麦当劳 MCP Token"     # 申请:https://open.mcd.cn/mcp
export PYTHONPATH=src

python -m mcdteam doctor                      # 自检:鉴权 / 工具 / 快照
python -m mcdteam --store 1950564 plan "8个人 预算220元 不吃辣 12点半送到"
```

### 环境要求

| 项目 | 要求 |
|---|---|
| Python | **3.9+**(仅使用标准库,**零第三方运行时依赖**) |
| 网络 | 接入真实 MCP 时需要;`--offline` 不需要 |
| Token | 到 [open.mcd.cn/mcp](https://open.mcd.cn/mcp) 控制台申请 |

> 零依赖是刻意设计的:评审只需 `python -m mcdteam ...` 就能复现全部结果,
> 不需要 `pip install` 任何东西。

### 让 Claude / Cursor / Cherry Studio 等 MCP 客户端直接使用

`mcp-config.example.json` 提供了脱敏后的配置模板(仅使用环境变量占位符):

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": { "Authorization": "Bearer ${MCD_MCP_TOKEN}" }
    }
  }
}
```

---

## 使用示例

### 例 1:最简——只有人数和预算

```bash
python -m mcdteam --store 1950564 plan "10个人 预算300"
```

### 例 2:带忌口与偏好(自然语言)

```bash
python -m mcdteam --store 1950564 plan \
  "周五中午14个人,预算480,不吃辣,有人不吃牛肉,两位素食,要低卡,12点送到,来点能分着吃的"
```

解析结果会在报告中回显,避免「算得对但理解错」:

```text
硬性(已强制满足): 不吃辣、不吃牛肉、素食
软性(已尽量满足): 低卡优先、需要可分食的小食/拼盘
```

### 例 3:命令行显式指定(适合脚本化)

```bash
python -m mcdteam --store 1950564 plan \
  --headcount 14 --budget 480 \
  --avoid spicy --avoid beef --prefer low_calorie \
  --at "12:30" --effort 96 \
  --markdown proposal.md --json plan.json
```

### 例 4:导出 Markdown 提案发给行政审批

```bash
python -m mcdteam --store 1950564 plan "20个人 预算700" --markdown 团餐提案.md
```

生成的 `团餐提案.md` 含需求理解、采购清单、费用明细、营养参考与**调用轨迹**,
可直接归档或粘进 IM。

### 例 5:查看门店菜单与性价比

```bash
python -m mcdteam --store 1950564 menu --role main --sort protein --limit 20
```

```text
编码          餐品                      角色        价格    热量   蛋白  蛋白/元
────────────────────────────────────────────────────────────────────────────────
9900015729    蘸酱麦麦脆汁鸡            小食       11.90     192     17    1.43
1100          巨无霸                    主食       25.50     513     27    1.06
...
```

### 例 6:自检与能力探针

```bash
python -m mcdteam doctor                 # 鉴权 + 工具 + 快照完整性
python -m mcdteam tools                  # 列出 35 个工具的必填参数
python -m mcdteam probe --store 1950564  # 逐个工具探测可用性,输出 probe-report.json
python -m mcdteam leaderboard            # 抓官方排行榜,看赛道拥挤度
```

---

## 命令参考

| 命令 | 作用 |
|---|---|
| `plan [文本]` | 生成团餐采购方案(核心命令) |
| `doctor` | 自检:Token 有效性、工具可用性、快照完整性、营养快照可解析性 |
| `menu` | 查看门店菜单、单价、营养与性价比排序 |
| `tools` | 列出 MCP Server 暴露的全部工具及其必填参数 |
| `probe` | 运行 MCP 能力探针,输出可复现的可用性报告 |
| `scan` | 提交前扫描仓库中是否残留真实凭据 |
| `agent-plan` | **Agent 专用**:stdout 只输出一个 JSON 信封,失败也返回结构化错误 |
| `order preview` | **装配下单请求但不提交**(dry-run),打印会提交什么 |
| `order pay` | **真实下单** —— 需 `--pay` + 环境变量 `MCD_ALLOW_REAL_ORDER=1` 双闸门 |
| `order status` / `order list` | 查询订单状态 / 历史订单(只读) |
| `order cancel` | 取消订单(有副作用,需确认) |
| `leaderboard` | 抓取官方排行榜(只读,用于评估赛道拥挤度) |

另有一个独立脚本用于**把演示快照换成你自己门店的数据**(快照只含门店编码、
餐品编码与价格,不含个人信息):

```bash
export MCD_MCP_TOKEN="..."
python tools/regen_fixtures.py --store 1950564 --out fixtures
```

### `plan` 的常用参数

| 参数 | 说明 |
|---|---|
| `--store <code>` | 门店编码(团餐/外送给定时仍需真实门店) |
| `--city <城市>` | 未指定门店时按城市搜索 |
| `--headcount <n>` | 人数 |
| `--budget <元>` / `--per-person <元>` | 总预算 / 人均预算 |
| `--mode` | `pickup` / `drive_thru` / `delivery` / `group` |
| `--at "12:30"` | 送达或取餐时间,支持 `12:30`、`2026-10-16 12:30` |
| `--avoid` | 硬性忌口,可重复:`spicy` `beef` `pork` `fish` `chicken` `egg` `dairy` `vegetarian` |
| `--prefer` | 软性偏好,可重复:`low_calorie` `high_protein` `low_sodium` `filling` `shareable` `coffee` `dessert` `refreshing` |
| `--max-kcal` / `--min-protein` | 人均热量上限 / 人均蛋白质下限 |
| `--effort <n>` | 搜索强度(重启次数),默认 48;预算紧张或人数多时建议提到 96~200 |
| `--markdown` / `--json` | 导出提案 / 结构化结果 |
| `--offline` | 全程使用仓库内置快照,不发网络请求 |
| `-v` | 输出每一步的调用日志与耗时 |

---

## 它是怎么算的

### 1. 问题定义

```text
minimize    cost(plan) + λ·deficit(plan) - μ·preference(plan) - ν·budget_use(plan)
subject to  cost(plan) ≤ 预算
            plan 中每件餐品都不违反任一硬性忌口
            plan 满足各角色的件数配额(主食配额 = 人数)
```

### 2. 为什么不用 ILP

带基数约束的多选择背包问题精确求解需要 ILP 求解器,但本项目坚持**零第三方依赖**,
因此采用「可行性过滤 + 随机多起点局部搜索」两步法:

1. **可行性过滤**:按硬性忌口剔除餐品,并剔除蘸酱/瓶装水这类不占配额的条目;
   若过滤后主食池为空,按 `辣 → 蛋 → 乳 → 鱼 → 猪 → 牛 → 鸡` 的顺序逐条让位,
   并把**每一次让位写进报告**——宁可少满足一条约束,也不能给出违反明确声明的方案。
2. **多起点局部搜索**:每个起点按配额随机装配(偏好加权抽样),随后反复执行
   「压回预算 → 换更便宜的同类 → 用余量补缺口/加体验项」,目标函数低者胜;
   重启 `--effort` 次取最优。相同 `random_seed` 下结果**完全可复现**。

### 3. 目标函数的每一项都有实测依据

| 项 | 权重 | 为什么这么设 |
|---|---|---|
| 主食缺口 | 6000 分/件(≈60 元) | 团餐最容易翻车的点;实测权重不足时求解器会给出「14 份冰淇淋配 2 个汉堡」 |
| 小食/饮料缺口 | 1200 / 400 分/件 | 软配额,允许在预算紧张时不足 |
| 软偏好加分 | 1 分 ≈ 1.2 元 | 让「好搭配」压过「纯凑便宜」 |
| 多样性惩罚 | 峰值占比超 50% 起罚 | 防止一次团餐全买成同一种最便宜的汉堡 |
| **预算利用率加分** | 成本的 35% | 团餐预算通常不退,实测没有这一项时求解器会为了省钱给人均 220 kcal 的方案 |

### 4. 服务端核价与自动收敛(本项目的关键差异点)

本地按「单价 × 数量」相加只是**估算**。门店促销、规格、取餐方式都会让实价不同
(实测同一份清单:本地估算 ¥214.50,门店实时 ¥231.70,差 ¥17.20)。

因此每次求解后都会:

1. 调 `calculate-price` 回算真实总价,并以服务端价为准;
2. 若回算后超预算,进入 `fit_plan_to_budget`:按
   **甜品 → 小食 → 饮料 → 超额主食** 的顺序逐份削减,每轮重新核价;
3. 主食是最后防线(削减到低于人数即停止),并把所有差异与削减动作
   写进报告的「需要你拍板的地方」。

```text
  ⚠️ 需要你拍板的地方
  · 为回到预算内,已减少 1 份「苹果片」
  · 门店实时原价 ¥223.40 与本地估算 ¥207.00 相差 ¥16.40(促销、规格或取餐方式导致),已以门店价为准
```

---

## 项目结构

```text
mcd-group-catering/
├── SKILL.md                      ★ Agent Skill 定义(角色/铁律/工作流/脚本契约)
├── README.md                     项目介绍、安装、使用示例、目标用户
├── CONTEST_DECLARATION.md        参赛声明(内容与官方模板一致)
├── MCP_INTEGRATION.md            实际使用的 MCP Server / Tool / 调用流程 / 踩坑实录
├── mcp-config.example.json       脱敏后的 MCP 配置(仅环境变量占位符)
├── install.ps1                   Windows 一键环境准备
├── LICENSE                       MIT
├── pyproject.toml                打包配置(无第三方运行时依赖)
├── scripts/                      ★ Agent 侧入口(stdout 纯 JSON,自动处理 PYTHONPATH)
│   ├── plan.py                       一句话需求 → 方案 JSON 信封
│   ├── check.py                      环境自检(必需文件 / Token / 连通性)
│   └── _bootstrap.py                 PYTHONPATH 自举 + UTF-8 输出
├── references/                   ★ Agent 按需查阅的参考资料
│   ├── toolchain.md                  参数链规则与九个实测坑
│   ├── planning-rules.md             配比公式、目标函数权重、让位与收敛规则
│   └── troubleshooting.md            故障排查步骤
├── fixtures/                     真实抓取的离线快照,保证一键可复现
│   ├── nutrition_raw.txt             160 条官方营养数据
│   ├── query-meals.1950564.json      门店 117 个在售编码与分类
│   ├── price-map.store1950564.json   批量取价结果
│   └── query-store-coupons.1950564.json
├── src/mcdteam/
│   ├── mcp/                      MCP 客户端、错误分层、门店上下文
│   │   ├── client.py                 限流 + 重试 + SSE/JSON 双解析 + 快照兜底
│   │   ├── context.py                orderType/beType/beCode 参数链规则
│   │   └── errors.py                 五类错误的用户提示
│   ├── data/                     数据层
│   │   ├── menu.py                   菜单索引、角色分类、批量取价策略
│   │   ├── nutrition.py              文本营养表格解析与模糊匹配
│   │   └── coupons.py                券/促销规则归一化
│   ├── solve/                    求解层
│   │   ├── constraints.py            中文需求解析 + 配比配额推算
│   │   └── planner.py                候选过滤 + 多起点局部搜索 + 核价收敛
│   ├── report/render.py          终端卡片 / Markdown 提案 / 群聊短卡片
│   ├── orchestrator.py           端到端流程编排
│   ├── order.py                  下单编排:三重安全闸门 + 金额闸门 + 请求装配
│   ├── probe.py                  MCP 能力探针(可复现的可用性报告)
│   ├── contest.py                官方排行榜抓取(只读)
│   └── cli.py                    命令行入口
├── tools/
│   └── regen_fixtures.py         用你自己的门店重新生成离线快照
├── tests/                        96 个单元测试
└── examples/                     示例输出
```

---

## 测试

```bash
python -m pytest -q
```

```text
...............................................................          [100%]
96 passed
```

覆盖范围:

| 测试文件 | 覆盖内容 |
|---|---|
| `test_nutrition.py` | 文本营养表格解析:BOM、含逗号的说明列、重复行、空返回、模糊匹配 |
| `test_menu.py` | 角色分类(含炸鸡/瓶装水/蘸酱/套餐等易错项)、份量推断、多编码合并、售罄标记 |
| `test_solver.py` | 中文需求解析、配额推算、券与促销归一化、参数链规则、求解器预算/忌口/主食配额/可复现性、团餐 gmServiceCode、套餐营养拆解与名称不一致防护、取价失败报错、报告渲染 |
| `test_skill_conformance.py` | Skill 形态合规:frontmatter 规范、触发词覆盖、参考文件存在性、脚本自举、必需文件齐全、声明的铁律与免责声明是否在册 |
| `test_agent_entrypoints.py` | 真的把脚本当子进程跑:stdout 是纯 JSON、失败返回结构化信封、`--json` 落盘可用 |

关键断言示例:

```python
def test_planner_respects_budget_and_main_quota():
    requirements = CateringRequirements(headcount=8, budget_total=200, effort=24)
    plan = CateringPlanner(_menu(), requirements).solve()
    assert plan.payable_cents <= requirements.budget_cents
    assert plan.role_counts().get("主食", 0) >= 8      # 每人一份主食
```

---

## 安全与隐私

- **Token 永不入仓**:只从环境变量 `MCD_MCP_TOKEN` 或 `--token` 读取;
  报告、日志、错误信息统一经 `redact()` 脱敏,展示时使用 `2U4m****D5Fz` 掩码。
- **默认不花钱**:只有 `create-order` 会真实下单,且该调用**禁用快照兜底**
   (`use_snapshot=False`),必须是实时结果;本项目默认只输出方案,不自动下单。
- **只读的排行榜抓取**:`leaderboard` 命令仅通过 GitHub 公开 contents 接口
  读取官方 `RANKING.md`,不做任何写入。
- **无第三方依赖**:不引入外部包,减少供应链风险。
- **个人信息**:仓库内含的门店快照仅包含门店编码、餐品编码与价格,
  **不含**任何用户地址、手机号、会员号或 Token。

---

## 常见问题

**Q:没有 Token 能跑吗?**
能。`--offline` 会使用仓库内置的真实快照跑完整链路(门店 1950564),
适合快速验证与评审复现。

**Q:为什么不直接调用 `create-order` 下单?**
下单涉及真实支付,应由使用者明确确认。本项目定位是**生成并核验方案**,
下单动作留给使用者执行;`create_order` 已封装好但不自动触发。

**Q:`缺少参数` 是什么问题?**
这是麦当劳 MCP 最误导的错误文案。实测三种情况都会返回它:
① 请求体编码不是 UTF-8 字节流;② `storeCode` 格式正确但门店不存在;
③ 团餐/外送/得来速场景漏传 `beCode`。详见 `MCP_INTEGRATION.md` 的「实测踩坑实录」。

**Q:营养数据为什么有缺口?**
`list-nutrition-foods` 只覆盖单品(160 条),套餐类基本缺失。本项目对缺失项
**不按 0 计入**,而是单独统计并在报告中提示为「下限值」。

**Q:结果能复现吗?**
能。求解器使用固定随机种子(`random_seed`,默认 20261025),
相同输入与相同菜单快照下输出完全一致,并有单元测试保证。

---

## 免责声明

本项目为**第三方开发的参赛作品,非麦当劳官方产品**。
餐品价格、可售状态与优惠券可用性以麦当劳官方渠道的实时结果为准;
营养数据来源于麦当劳 MCP 的公开接口,仅供参考,**不构成医疗、营养或健康建议**。
本项目遵循麦当劳 MCP 服务规则,仅用于非商业用途的参赛演示。
麦当劳及相关商标归其权利人所有。

## 许可

[MIT](LICENSE)
