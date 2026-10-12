# 麦当劳 MCP 集成说明

> 本文档记录本项目**实际调用**的麦当劳 MCP Server、Tool、调用流程与业务价值,
> 并附上开发过程中实测到的接口行为与踩坑结论,供其他开发者复用。
> 所有结论均来自对 `https://mcp.mcd.cn` 的真实调用(2026-10 实测)。

---

## 一、接入信息

| 项目 | 值 |
|---|---|
| Server | 麦当劳中国 MCP Server(`serverInfo.name = mcd-mcp`, `version = 1.0.0`) |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP(单端点 POST) |
| 协议版本 | `2025-06-18` |
| 鉴权 | 请求头 `Authorization: Bearer <MCD_MCP_TOKEN>` |
| 请求头要求 | `Content-Type: application/json; charset=utf-8`、`Accept: application/json, text/event-stream` |
| 平台限流 | 600 次/分钟,超限返回 HTTP 429 |
| 实测工具数 | `tools/list` 返回 **35 个**(官方 README 列出 33 个,多出 `query-promotions` 与 `query-survey-coupon`) |

本项目在 `src/mcdteam/mcp/client.py` 中实现了零第三方依赖的客户端,
内置 **滑动窗口限速(默认 540 次/分钟)+ 429/5xx 指数退避 + SSE/JSON 双解析**。

---

## 二、本项目使用的 Tool 清单

按调用顺序排列。✅ 表示本项目的核心链路依赖它。

| # | Tool | 用途 | 关键入参 | 本项目用法 |
|---:|---|---|---|---|
| 1 | `now-time-info` ✅ | 服务器时间 | — | `doctor` 自检、校准预约时间 |
| 2 | `query-nearby-stores` ✅ | 到店/得来速门店 | `beType`(1/5)、`searchType`(1/2)、`city`、`keyword` | 按城市解析门店;`beType=5` 时取回 `beCode` |
| 3 | `delivery-query-stores` ✅ | 外送/团餐门店 | `addressId`、`beType`(2/6) | 团餐场景解析门店与 `beCode` |
| 4 | `delivery-query-addresses` ✅ | 配送地址 | — | 自动选取默认地址作为团餐配送点 |
| 5 | `query-meals` ✅ | 门店在售餐品 | `storeCode`、`orderType`、`beType`、`beCode?` | 拿到 117 个可售编码与分类/标签,作为菜单索引的骨架 |
| 6 | `calculate-price` ✅ | 价格计算 | `storeCode`、`items[]`、`orderType`、`beType` | ① 批量反查菜名与单价 ② 回算方案真实总价 ③ 超预算时逐轮重算 |
| 7 | `list-nutrition-foods` ✅ | 餐品营养 | — | 解析 160 条营养记录,挂到菜单上 |
| 8 | `query-meal-detail` ✅ | 餐品组成 | `storeCode`、`code`、`orderType`、`beType` | **拆解套餐营养**:把默认组成子项按名称匹配营养表后累加 |
| 9 | `query-store-coupons` ✅ | 门店可用券 | `storeCode`、`orderType`、`beType` | 取券并叠加到方案上 |
| 10 | `query-my-coupons` ✅ | 卡包券 | `page`、`pageSize` | 兜底补充可用券 |
| 11 | `query-promotions` ✅ | 团餐满减/满折 | `storeCode`、`beCode`、`beType=6`、`orderType=2` | 企业团餐场景解析促销规则 |
| 12 | `query-meal-assistance` ✅ | 团餐助餐服务 | `storeCode`、`orderType=2`、`beType=6` | **取 `gmServiceCode`**:团餐取价与下单的前置条件 |
| 13 | `campaign-calendar` | 活动日历 | — | 提示当月在进行的活动 |
| 14 | `create-order` ✅ | 下单 | `storeCode`、`items[]`、`takeWayCode`/`addressId`、`beCode?`、`gmServiceCode?` | **仅在用户三重闸门全开后调用**,不使用快照兜底 |
| 15 | `query-order` ✅ | 查询订单状态 | `orderId` | 只读,下单后的状态确认 |
| 16 | `order-list` ✅ | 历史订单 | — | 只读,`order list` 子命令 |
| 17 | `cancel-order` ✅ | 取消订单 | `orderId`、`cancelReasonCode` | 有副作用,需用户确认 |

其余工具(`mall-*`、`party-*`、`draw-lottery` 等)与本项目的团餐场景无关,未接入。

---

## 三、调用流程

团餐方案的实际请求编排(每一步都记录了耗时与失败原因,可在报告的「调用轨迹」里查看):

```
                    ┌──────────────────────────────┐
                    │ 自然语言需求(中文)           │
                    │ 「14人 预算480 不吃辣…」      │
                    └──────────────┬───────────────┘
                                   ▼
        parse_requirements ──► CateringRequirements(人数/预算/时间/忌口/偏好)
                                   │
   ┌───────────────────────────────┴────────────────────────────────┐
   ▼ ① 门店解析                                                     │
   query-nearby-stores / delivery-query-stores                      │
     └─► storeCode + beCode + 营业状态 + 预约时段                    │
   ▼ ② 菜单索引                                                     │
   query-meals ──► 117 个 productCode + 分类 + 标签                  │
   ▼ ③ 批量取价(6 个一批 + 失败逐条重试)                          │
   calculate-price × ~20 ──► 116/117 个菜名与单价                    │
   ▼ ④ 营养挂载(内置重试 + 快照兜底)                              │
   list-nutrition-foods ──► 160 条营养 → 归一化名称匹配              │
   ▼ ⑤ 优惠资产                                                     │
   query-store-coupons + query-my-coupons [+ query-promotions]      │
   ▼ ⑥ 约束求解(本地,零依赖)                                     │
   build_candidate_pool ──► 多起点局部搜索 ──► CateringPlan          │
   ▼ ⑦ 服务端核价与收敛                                             │
   calculate-price ──► 实价覆盖本地估算 ──► 超预算则削减并重算        │
                    └──────────────────────────────────────────────┘
```

### 工具间的「参数链」规则(必须严格遵守)

`orderType` / `beType` / `beCode` 三者是强绑定的,传错会被服务端以
`缺少参数` **静默拒绝**:

| 取餐方式 | orderType | beType | beCode | 门店接口 |
|---|:---:|:---:|---|---|
| 到店自取(堂食/外带) | 1 | 1 | 不传 | `query-nearby-stores`(beType=1) |
| 得来速车道取餐 | 1 | 5 | **必传** | `query-nearby-stores`(beType=5) |
| 麦乐送到家 | 2 | 2 | **必传** | `delivery-query-stores`(beType=2) |
| 企业团餐 | 2 | 6 | **必传** | `delivery-query-stores`(beType=6) |

企业团餐还额外需要 `gmServiceCode`(取自 `query-meal-assistance`),
缺失时 `calculate-price` / `create-order` 会回「团餐助餐服务参数缺失」——
详见下文「实测踩坑实录」第 8 条。

本项目把这张表编码成 `mcdteam.mcp.context.FulfillmentMode` 与 `StoreContext`
(`store_arguments()` 给门店类工具用,`price_arguments()` 额外带上助餐服务码),
并有单元测试覆盖(`tests/test_solver.py::test_store_context_*`)。

### `code` → `productCode` 的字段映射(最容易静默出错的地方)

`query-meals` 返回的商品标识字段名是 **`code`**,
而 `calculate-price` / `create-order` 需要的是 **`items[].productCode`**。
两者必须是同一个值。若传错字段名或传错值:

* 服务端**不报错**;
* 返回的 `price` 直接是 `0`。

本项目在 `data/menu.py::collect_price_map` 中显式做了映射,
并用「取价成功率」自检(实测 117 个编码中 116 个可成功取价,
剩余 1 个 `521743` 为真实下架商品)。

---

## 四、实测踩坑实录

以下每一条都是本项目开发过程中**真实卡住过**的问题,已固化为代码或探针
(`python -m mcdteam.probe`),供社区复用。

### 1. 请求体编码方式会决定「成功」还是「缺少参数」

同一个 JSON-RPC 请求体:

* 用 `json.dumps(...).encode("utf-8")` 显式转字节流 → 成功;
* 用某些运行时默认编码(或带 BOM)发送 → 服务端回
  `{"success":false,"code":400,"message":"缺少参数"}`。

服务端不会指出是哪个参数缺失。因此客户端统一使用:

```python
body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
```

### 2. 门店级工具必须传**有效的** storeCode

传一个格式正确但不存在的编码(如 `123456789`)时,`query-meals` /
`calculate-price` / `query-store-coupons` 统一返回 `缺少参数`,
而**不是**「门店不存在」。这个误导性的错误文案会让人误以为是参数名写错了。

### 3. `list-nutrition-foods` 返回的是文本表格,不是 JSON

实际结构:

```
[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
```

三个坑:

* `nutritionDescription` 可能是 `null`,也可能是**含逗号**的说明
  (如 `能量约鲈鱼1条`),不能简单按下标切片;
* 存在重复行(如 `小杯玉米杯`、`纯牛奶（盒装）`);
* 该接口**偶发返回空内容**(社区 issue #16 亦有记录),
  本项目做了「重试 3 次 → 回落本地快照」的双保险。

解析器见 `data/nutrition.py::parse_nutrition_table`,测试见 `tests/test_nutrition.py`。

### 4. 一次性传入全部商品编码会被整批拒绝

把 117 个 `productCode` 一次性丢给 `calculate-price`,服务端返回
`商品已售罄或不可售,换一个商品或检查orderType/beType`,**整批失败**。

本项目采用「6 个一批 + 失败批次逐条重试」:

| 策略 | 取价成功数 | 成功率 |
|---|---:|---:|
| 一次传入 117 个 | 0 / 117 | 0% |
| 6 个一批 + 逐条重试 | 116 / 117 | 99.1% |

### 5. 门店实价与菜单快照价会不一致

`query-meals` 只给编码,价格必须靠 `calculate-price` 反查。而门店促销、
规格、取餐方式变化都会让**不同时刻的价格不同**。实测同一份清单:
本地快照合计 ¥214.50,门店实时原价 ¥231.70(差 ¥17.20)。

因此本项目坚持:

1. 方案生成后**必须**用 `calculate-price` 回算,并以服务端价为准;
2. 回算后若超出预算,进入**自动收敛**流程(`fit_plan_to_budget`),
   按「甜品 → 小食 → 饮料 → 超额主食」的顺序逐份削减,每轮重新核价;
3. 所有差异都写进报告的「需要你拍板的地方」,不做静默处理。

### 6. 同一餐品存在多个编码

实测 `鱼排堡开心乐园餐`(9900005421 / 9900016058)、
`汉堡开心乐园餐`(9900005422 / 9900016057)各有两个编码。
不去重会让求解器把同一餐品当两种选择重复推荐,因此
`build_catalog` 与 `build_candidate_pool` 都按归一化菜名做了合并。

### 7. 官方营养数据不覆盖套餐

`list-nutrition-foods` 的 160 条记录只覆盖单品;菜单里 117 个编码中约
60%~70% 能直接匹配到营养数据,套餐类基本缺失。

**本项目的解法:用 `query-meal-detail` 拆解套餐组成后累加。**

* 详情接口会返回 `rounds[].choices[]`,默认选中的子项(如
  `汉堡包`、`迷你薯条`、`纯牛奶`)都是单品,**按名称能命中官方营养表**;
* 这些子项编码**不能**直接下单(实测 `calculate-price` 会回
  「商品已售罄或不可售」),所以只用于营养计算,不参与价格;
* 判定「含多件」的依据是「至少两个 round 里选到了东西」,而不是角色分类 ——
  实测「汉堡开心乐园餐」会被推断成**主食**角色,用角色判断会漏掉。

效果对比(10 人素食团餐,主食全部是含多件的乐园餐):

| 方案 | 人均热量 | 说明 |
|---|---:|---|
| 不做拆解 | 38 kcal | 只有饮品与小食有数据,数值完全不可用 |
| 拆解后 | 459 kcal | 覆盖到主食,量级正常 |

### 8. 团餐的 `calculate-price` / `create-order` 必须带 `gmServiceCode`

企业团餐(`beType=6`)场景下,即使 `storeCode` / `beCode` 都对,
`calculate-price` 仍会返回::

    {"success":false,"code":600061,"message":"团餐助餐服务参数缺失"}

正确顺序是:先 `query-meal-assistance` 拿到助餐服务,
把 `selected=true` 且 `enable=true` 的那一项的 `gmServiceCode`
(例如 `GMS001 保鲜速达`)透传给 `calculate-price`。

**注意 `query-meals` 在没有 `gmServiceCode` 时仍能正常返回菜单**,
所以这个错误只会在取价阶段暴露 —— 如果再叠加「取价失败静默跳过」,
最终表现是一份「合计 ¥0.00」的空方案,极难定位。本项目因此:

1. 选店后立即解析并缓存 `gmServiceCode`;
2. 取价全军覆没时**抛出明确异常**而不是返回空表
   (`collect_price_map(..., raise_on_total_failure=True)`)。

### 9. 同一编码在不同接口可能返回不同名称

实测编码 `9900015602`:

* `calculate-price` 返回的名称是 **「牛肉堡单品精选」**;
* `query-meal-detail` 返回的名称是 **「大堡口福三件套」**(含 3 个 round)。

如果直接按详情拆解营养,就会给「牛肉堡单品精选」贴上三件套的营养。
本项目的处理:当详情是多件套餐、且名称与本地菜单明显不符时,
**放弃拆解并在报告中告警**(`以下编码在「价格接口」与「详情接口」返回的名称不一致`),
以门店实际商品为准,不做静默猜测。

### 10. 门店与配送地址不匹配时,只有 `create-order` 会失败

实测把北京门店 `1950564` 与无锡地址组合时:

| 步骤 | 结果 |
|---|---|
| `delivery-query-stores`(该地址) | 返回的是**无锡**的门店列表 |
| `query-meals(1950564, ...)` | ✅ 成功 |
| `calculate-price(...)` | ✅ 成功,返回 ¥38.20 |
| 请求装配(含 `addressId`) | ✅ 成功 |
| **`create-order(...)`** | ❌ `无法配送到当前地址,请选择其他地址` |

也就是说,错误只在最后一步暴露,前面每一步都"看起来正常"。

本项目的处理:在下单预览阶段用 `delivery-query-stores` 反查该地址的可配送门店,
确认目标 `storeCode` 在列表内;不在则**拦住提交并列出可配送门店**。实测提示:

```
⚠ 门店 1950564 不在该地址的可配送门店列表中,`create-order` 会返回
  「无法配送到当前地址」。该地址可配送的门店:麦当劳无锡云蝠大厦餐厅(1990684)、
  麦当劳无锡五爱路餐厅(1990572)、麦当劳无锡永乐路餐厅(1990041)
```

### 11. 未支付订单可以安全撤销(但**不存在**沙箱环境)

麦当劳 MCP 没有提供测试环境,所以本项目在开发阶段采用
「创建真实订单 → 查询 → 取消 → 复验」的方式做端到端验收,**全程不支付**。实测:

* `create-order` 返回 `orderDetail.orderStatus = 待支付`,状态码 `10`;
* `orderDetail.expirePayTime` 给出支付截止时间(**约 15 分钟后**),
  逾期订单自动关闭且不产生扣款;
* `cancel-order(orderId, cancelReasonCode)` 返回 `{"cancelResult": true}`,
  随后状态变为 `订单已取消`(状态码 `60`);
* 支付链接字段是 **`payH5Url`**(不是 `payUrl`)。

这套流程验证了参数装配、支付链接与状态流转都真实可用。
但请注意:**这仍然是生产环境的真实订单**,只是未支付而已 ——
不能把它当作可以随便试的沙箱,涉及真实金额与门店库存时仍需谨慎。

---

## 五、业务价值

### 场景

企业团餐 / 部门加班餐 / 会议茶歇的统筹者(行政、HR、助理)通常面对:

* **人数与预算**:14 人、总预算 480 元,人均不能超;
* **忌口与过敏**:1 人素食、1 人乳糖不耐、1 人过敏牛肉、若干人不吃辣;
* **时间窗**:中午 12:30 前必须送到;
* **配比**:既要每人有主食,又要小食可分食,还不能重复到吃腻。

现状是:人工在 App 里反复比价、算人均、对忌口,单次约 20~40 分钟,
且经常出现「小食买多了主食不够」「券没用上」「超预算」三类问题。

### 本项目解决的问题与量化收益

| 问题 | 人工现状 | 本项目的处理 |
|---|---|---|
| 预算控制 | 靠心算,经常超 | 约束求解保证预算内;**实价超预算时自动收敛**并重算 |
| 忌口遗漏 | 容易漏掉某一个人的禁忌 | 硬约束过滤;若因此无主食可用,**按优先级让位并明确告警** |
| 配比失衡 | 小食一堆、主食不够 | 主食配额 = 人数(硬约束),小食/饮料按人数比例推算 |
| 优惠用不上 | 结账时才发现有券 | 自动匹配门店券 + 卡包券,只接受真正降低应付的券 |
| 营养不透明 | 无数据 | 挂官方 160 条营养数据,给出整单/人均热量与蛋白质 |
| 反复比价耗时 | 20~40 分钟 | 一次调用完成,典型 30~60 秒(含 20 次批量取价) |

### 真实运行结果(2026-10-12,门店 1950564 北京建外 SOHO A 座)

```
需求:8个人 预算220元 不吃辣 要低卡 12点半送到
结果:应付 ¥219.00(人均 ¥27.38)  预算余量 ¥1.00  预算使用率 100%
      主食 1 份/人 · 饮料 每 1.3 人 1 份 · 小食 每 4 人 1 份
      人均 283 kcal / 14 g 蛋白质,已自动叠加门店券省 ¥5.20
      实价高于快照 ¥16.40,已自动削减 1 份小食回到预算内
```

### 与其他工具类 Skill 的差异

榜单上多数作品解决的是**单人在某一家店的决策**(选什么、省多少、热量多少)。
本项目的差异在于**多人多约束的批量统筹**:

* 决策单元是「一份 14 人的采购清单」,而不是「一个人的一顿饭」;
* 约束来源是**多人的禁忌并集**(有人不吃辣 + 有人素食 + 有人乳糖不耐);
* 用的是团餐专属链路(`beType=6`、`query-promotions`、`query-meal-assistance`),
  这条链路上目前的公开作品很少;
* 输出物是**给行政审批用的提案**(Markdown)+ **给同事群看的短卡片**,
  而不是一句推荐语。

---

## 六、工程实现要点

| 关注点 | 实现 |
|---|---|
| 零第三方依赖 | 仅标准库;`urllib` 手写 JSON-RPC,`json` 手写解析 |
| 限流与重试 | 滑动窗口 540 次/分钟;429/5xx/超时指数退避(0.6s→1.2s→2.4s) |
| 错误分层 | `MCPAuthError` / `MCPRateLimitError` / `MCPBusinessError` / `MCPProtocolError` / `MCPTransportError` |
| 业务码检查 | 强制检查 `structuredContent.success`,避免「HTTP 200 + 业务失败」被当成成功 |
| 离线可复现 | `fixtures/` 内置真实快照;`--offline` 全程不发网络请求 |
| 资金安全 | **默认 dry-run**;真实下单需 `--pay` + 环境变量 `MCD_ALLOW_REAL_ORDER=1` 双闸门;提交前重新核价,金额超容差(¥1.00)默认拒绝;`create-order` 禁用快照兜底 |
| 密钥安全 | Token 只从环境变量/命令行读取,报告与日志统一 `redact()` 脱敏 |
| 可测试性 | 129 个单元测试;`transport` 可注入,便于 mock |

---

## 七、后续计划

1. **团餐促销精确寻优**:`query-promotions` 的 `ruleDetail` 原始结构尚未文档化,
   目前已做宽口径解析;后续用真实规则样本反推字段语义,接入满减的精确寻优。
2. **企业微信 / 飞书机器人**:把 `plan` 子命令封装成机器人指令,
   让行政在群里直接下单。
3. **历史偏好学习**:基于 `order-list` 统计团队口味分布,
   把「上次大家吃剩了什么」纳入下一次的配比搜索。
4. **多门店比价**:同一地址下比较多家可配送门店的总价与配送时效。

---

## 八、免责声明

本项目为第三方开发的参赛作品,**非麦当劳官方产品**。
餐品价格、可售状态、优惠券可用性以麦当劳官方渠道的实时结果为准;
营养数据来源于麦当劳 MCP 的公开接口,仅供参考,**不构成医疗、营养或健康建议**。
本项目遵循麦当劳 MCP 服务规则,仅用于非商业用途的参赛演示。
