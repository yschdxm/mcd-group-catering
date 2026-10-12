# 拉票与推广素材包

> 比赛排名按 **Star 数** 计算,报名成功只是入场券。
> 这个文件提供可直接复制使用的推广文案,以及「该发到哪里」的建议。
>
> **发之前请务必:自己真实用过、按你的真实体验修改措辞。**
> 刷榜、机器人刷 Star、多账号互刷都属于活动规则明令禁止的行为,
> 一旦被判定会直接取消资格,得不偿失。

---

## 一、先做这三件事(5 分钟)

1. **自己跑一遍离线演示**,确认输出正常:

   ```bash
   git clone https://github.com/yschdxm/mcd-group-catering.git
   cd mcd-group-catering
   export PYTHONPATH=src
   python -m mcdteam --offline --store 1950564 plan "8个人 预算220元 不吃辣"
   ```

2. **把仓库 About 填好**(GitHub 仓库页右上角 ⚙️):
   - Description:`🍟 一句话中文需求 → 不超预算、不踩忌口、已叠加优惠券的企业团餐采购提案 | 麦当劳 MCP × 约束求解`
   - Topics:`mcp` `mcdonalds` `catering` `group-order` `optimization` `python` `llm-agent` `skill`

3. **确认 README 首屏能打动人**:打开仓库首页,只看前 20 行能否让人明白
   「这是什么、解决谁的什么问题」。当前 README 已按此标准排版。

---

## 二、可直接使用的推广文案

### A. V2EX / 掘金 / 少数派(偏技术,讲实现)

> **标题**:用约束求解给公司订麦当劳团餐:14 人 / 480 预算 / 有人不吃辣有人素食
>
> 最近在折腾麦当劳中国的 MCP Server,顺手写了个团餐统筹工具。
> 场景是行政/助理每周都要面对的那类问题:「周五中午 14 个人,预算 480,
> 两人减脂,一个不吃辣,一个不吃牛肉,还要能分着吃」。
>
> 人工做这件事大概要 20~40 分钟,还经常翻车在小食买多了、主食不够、
> 券忘了用、超预算这几件事上。
>
> 我的做法是把「多人禁忌的并集 + 预算 + 每人一份主食」建成约束,
> 用多起点局部搜索求解,再用 `calculate-price` 把实价回算一遍 ——
> 因为门店实时价和菜单快照价会差出十几块,不回算就是假账。
> 超出预算还会自动往回收敛(按甜品→小食→饮料→主食的顺序削,每轮重新核价)。
>
> 开发过程中踩了不少坑,都写进 MCP_INTEGRATION.md 了,举两个:
> 1. 请求体的编码方式会决定服务端认不认你的参数 —— 用非 UTF-8 字节流发,
>    会统一回你「缺少参数」,也不告诉你是哪个参数错了;
> 2. `list-nutrition-foods` 返回的不是 JSON,是一段自定义文本表格,
>    而且说明列里可能带逗号,不能按下标硬切。
>
> 零第三方依赖(纯标准库),内置真实门店快照,`--offline` 就能一键复现,
> 63 个单元测试。项目是开源的,觉得有用的话求个 Star ⭐
> https://github.com/yschdxm/mcd-group-catering

### B. 小红书 / 即刻 / 微博(偏生活,讲效果)

> **标题**:再也不用为「部门团餐订什么」吵半小时了🍟
>
> 我们部门 14 个人,每次订麦当劳都要在群里拉扯:
> 有人不吃辣、有人不吃牛肉、有两位在减脂、预算还卡死在 480。
>
> 我写了个小工具,现在只要输入一句话👇
> 「14个人 预算480 不吃辣 两个减脂 12点送到 要能分着吃的」
>
> 它 30 秒给我一份清单:
> ✅ 主食正好每人一份(不会出现小食一堆、汉堡不够吃)
> ✅ 自动避开所有忌口
> ✅ 自动叠加门店优惠券,这次省了 5.2 元
> ✅ 门店实价超预算会自动砍掉一份小食,砍完重新算一遍
> ✅ 还给出人均热量和蛋白质,减脂的同事心里有数
>
> 附了营养参考,但**不构成营养建议**,就是让大家点得明白点。
> 开源免费:https://github.com/yschdxm/mcd-group-catering
> 顺手点个 Star 支持一下⭐⭐

### C. 微信群 / 钉钉群(短,一句话版本)

> 分享个开源小工具,专治「部门团餐订什么」:
> 一句话需求 → 不超预算、避开所有人忌口、自动用券的采购清单,
> 主食保证每人一份,门店实价超预算还会自动收敛。
> 麦当劳 MCP 开发,零依赖可离线复现:https://github.com/yschdxm/mcd-group-catering
> 有用的话求个 Star ⭐

### D. Reddit / Hacker News(英文,讲技术)

> **Title**: I built a constraint solver for office McDonald's group orders (MCP + Python, zero deps)
>
> Office catering has a specific failure mode: you buy too many sides, not enough
> mains, forget the coupons, and blow the budget. Add "one person is vegetarian,
> one is lactose intolerant, two don't eat spicy" and it gets genuinely annoying.
>
> I turned it into a constrained optimization problem over McDonald's China's MCP
> Server (35 tools). Hard dietary rules filter the candidate pool, mains quota is
> pinned to headcount, and a multi-start local search minimizes cost while
> rewarding variety and preference match. Then `calculate-price` re-quotes the
> whole basket — store prices legitimately differ from menu snapshots by >$2, so
> the solver auto-shrinks the basket and re-quotes until it fits the budget.
>
> Zero third-party dependencies (stdlib only), real store snapshots bundled so
> `--offline` reproduces everything, 63 unit tests.
> Write-up of the API gotchas: MCP_INTEGRATION.md.
> Star if useful: https://github.com/yschdxm/mcd-group-catering

---

## 三、发到哪里

| 渠道 | 适合的文案 | 备注 |
|---|---|---|
| V2EX `程序员` / `分享创造` 节点 | A | 标题党适度,正文要有干货 |
| 掘金 / 少数派 / 知乎 | A | 可配上终端截图 |
| 小红书 | B | 配图是终端输出的截图效果最好 |
| 即刻 `程序员` 圈子 | B/C | 短平快 |
| 微博 + 话题 `#麦当劳` `#程序员节` | B | 可 @ 麦当劳中国相关账号(不要冒用官方名义) |
| 公司/学校的技术群、部门群 | C | 真实用户就在身边,转化率最高 |
| Reddit `r/MCP` `r/LocalLLaMA` | D | 英文社区对 MCP 话题接受度高 |
| GitHub Topics | 全部 | 给仓库加 `mcp` `mcdonalds` `catering` 等 topic 能带来自然流量 |

### 发帖节奏建议

- **第 1 天**:V2EX + 掘金(技术社区,容易拿到第一批 Star)
- **第 2 天**:小红书 + 即刻 + 微信群(生活场景,覆盖面广)
- **第 3 天**:Reddit / HN(如果需要海外流量)
- **后续**:在评论区回答问题,**每条认真回复的评论都可能带来一次转发**

---

## 四、要避免的坑(活动规则明确禁止)

- ❌ 买 Star、用机器人刷 Star、脚本互刷;
- ❌ 同一人注册/控制多账号互点;
- ❌ 抄袭他人项目;
- ❌ 在文案里暗示与麦当劳官方有关联(必须写明「第三方作品,非官方产品」);
- ❌ 贬低其他参赛作品或把麦当劳产品与其他品牌做对比。

以上任意一条都可能被取消参赛与获奖资格。**老老实实做产品、认认真真写文案**就够了。

---

## 五、如果被问到这些问题

**Q:和榜单上那些「省钱助手」有什么不同?**
A:那些解决的是**单人在一家店**的决策;本项目解决的是**多人多约束的批量统筹** ——
决策单元是一份 14 人的采购清单,约束是多人禁忌的并集,用的是团餐专属链路
(`beType=6` / `query-promotions` / `query-meal-assistance`)。

**Q:为什么不用 ILP 精确求解?**
A:为了坚持零第三方依赖(评审无需 `pip install`)。因此用「可行性过滤 +
随机多起点局部搜索」,相同随机种子下结果完全可复现,并有单元测试保证。

**Q:价格准吗?**
A:本地按单价相加只是估算,方案生成后一定用 `calculate-price` 回算并以门店实价为准;
差异会写进报告。实测同一份清单本地估算 ¥214.50、门店实价 ¥231.70,差 ¥17.20。

**Q:营养数据可靠吗?**
A:来自麦当劳 MCP 的官方接口 160 条记录,覆盖约 60%~70% 的单品;
匹配不到的餐品**不按 0 计入**,而是标注为「下限值」。仅供参考,不构成营养建议。
