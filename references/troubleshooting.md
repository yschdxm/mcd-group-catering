# 故障排查(Agent 用)

> 遇到问题时**按顺序**排查,不要跳步、不要靠猜。
> 每一步都能单独验证,避免把环境问题和业务问题混在一起。

---

## 第 0 步:永远先跑自检

```bash
python scripts/check.py
```

它会一次性告诉你:必需文件是否齐全、Token 是否就绪、能否连上 MCP、快照是否完整。

**退出码 0 = 就绪;1 = 有问题并已列出原因。**

---

## 一、`缺少参数`(最常见的报错)

这是麦当劳 MCP **最误导**的错误文案。以下三种情况都会返回它:

| 原因 | 怎么确认 | 怎么修 |
|---|---|---|
| `storeCode` 格式对但门店不存在 | 换一个确定存在的门店编码重试 | 用 `--city` 让脚本重新搜店,或从麦当劳 App 里核对门店编码 |
| 团餐/外送/得来速漏传 `beCode` | 看 `context.storeContext.beCode` 是否为空 | 让脚本自己解析、不要手工指定 `--be-code` |
| 企业团餐漏传 `gmServiceCode` | 报错文案通常是 `600061 团餐助餐服务参数缺失` | 需要 `query-meal-assistance`;本项目已自动处理,若仍报错见第四节 |

**不要**通过反复改参数试错。先确认门店是否真实存在。

---

## 二、金额是 `0` 或清单为空

| 现象 | 原因 | 处理 |
|---|---|---|
| `status=error` + `error.type=empty_plan` | 预算过低,或忌口过滤后无可用餐品 | 提高预算、减少 `--avoid`,或提高 `--effort` |
| `error.type=business_rejected` 且提到取价 | 参数链不匹配(见第一节) | 按第一节排查 |
| 方案非空但 `subtotalYuan=0` | 理论上不会出现;若出现说明 `collect_price_map` 保护失效 | 直接报告为 bug,并附上 `context.calls` |

**本项目已保证**:取价全军覆没时**不会**静默产出空方案,而是抛出明确错误。

---

## 三、`error.type=missing_token` / `auth_failed`

```
error.type = missing_token  →  环境变量 MCD_MCP_TOKEN 未设置
error.type = auth_failed    →  Token 无效或已过期
```

**修复步骤**:

1. 打开 https://open.mcd.cn/mcp
2. 右上角【登录】→ 手机号验证
3. 点【控制台】→【激活】→ 同意服务协议 → 一键复制 Token
4. 设置环境变量:

   ```powershell
   # Windows PowerShell
   $env:MCD_MCP_TOKEN = "你的Token"
   ```

   ```bash
   # macOS / Linux
   export MCD_MCP_TOKEN="你的Token"
   ```

5. 重新跑 `python scripts/check.py` 确认

> 若用户暂时拿不到 Token,可先用 `--offline` + `--store 1950564` 跑内置快照演示,
> 但**必须**告知用户:离线模式的价格来自快照、未实时核价,营养数据也不含套餐拆解。

---

## 四、团餐场景报「助餐服务参数缺失」

企业团餐(`beType=6`)需要 `gmServiceCode`。本项目已自动处理:

1. 选店后调用 `query-meal-assistance`
2. 取 `enable != false` 且 `selected == true` 的那一项(例如 `GMS001 保鲜速达`)
3. 自动透传给 `calculate-price`

**若仍然报错**,检查 `context.storeContext.gmServiceCode` 是否为空。
为空通常意味着 `query-meal-assistance` 也失败了 —— 那说明门店/`beCode` 本身就不对。

> 注意:`query-meals` 在没有 `gmServiceCode` 时**仍能成功返回菜单**,
> 所以这个错误只会在取价阶段暴露。

---

## 五、营养数据看起来严重偏低

看 `plan.warnings` 里有没有这两条:

| 提示 | 含义 | 你要怎么说 |
|---|---|---|
| `有 N 件餐品缺少官方营养数据,营养合计为下限值而非精确值` | 部分餐品无营养数据,未按 0 计入 | 说明合计是下限值 |
| `本单所有主食(均为套餐类)都没有官方营养数据…数值严重偏低` | 主食全是套餐,营养完全缺失 | **必须**强调该数值不可用,而不是照读 |

**离线模式(`--offline`)下这个情况是预期的**,因为无法调用 `query-meal-detail`。

在线模式下若出现,说明套餐拆解也失败了,检查 `context.calls` 里有没有
`query-meal-detail` 的记录。

---

## 六、价格与预期不符 / 超预算

这是**正常现象**,不是 bug。看 `plan.warnings`:

```
门店实时原价 ¥231.70 与本地估算 ¥214.50 相差 ¥17.20(促销、规格或取餐方式导致),已以门店价为准
为回到预算内,已减少:苹果片×2
```

| 字段 | 含义 |
|---|---|
| `plan.localEstimateYuan` | 本地按单价估算的原价 |
| `plan.subtotalYuan` | 门店实时原价(已核价时以它为准) |
| `plan.verifiedByApi = false` | **金额是估算**,必须向用户说明 |
| `plan.warnings` 里的 `auto_cut` | 已自动削减的条目 |

**你必须把 `warnings` 原文转述给用户**,不得为了方案好看而隐去。

---

## 七、价格合理但计算结果每次都一样/不一样

- **一样**:正常。求解器用固定随机种子,相同输入必然相同结果。
- **不一样**:检查菜单快照是否变了(门店上下架),或 `--effort` 不同。

---

## 八、报错信息收集清单(向用户反馈 bug 时附上)

若确实疑似 bug,请附上:

1. 完整命令行(去掉 Token)
2. `envelope.error` 全文(含 `type` / `message` / `tool` / `traceId`)
3. `envelope.context.calls`(每步调用的状态码与耗时)
4. `python scripts/check.py` 的输出

`traceId` 是麦当劳服务端的问题追踪号,是他们定位问题的关键。

---

## 九、下单相关(⚠️ 有真实副作用)

麦当劳 MCP **没有测试环境/沙箱**。`create-order` 一旦成功就会产生真实待支付订单。

### 9.1 下单被拦:"要真实下单需同时满足…"

这是**设计如此**,不是 bug。三道闸门必须同时满足:

| 闸门 | 怎么开 |
|---|---|
| 1. 用户明确确认 | 用户说出"下单""确认"之类的话 |
| 2. 命令行 `--pay` | `order pay ... --pay` |
| 3. 环境变量 | `MCD_ALLOW_REAL_ORDER=1` |

**Agent 不得替用户设置环境变量 3** —— 它存在的意义就是让用户亲自按下扳机。
正确做法:告诉用户需要设这个变量,并解释原因(没有测试环境、会产生真实订单)。

### 9.2 报错"到店/得来速场景必须提供 takeWayCode"

`calculate-price` 没有返回 `takeWayList`。可能原因:

- `orderType` / `beType` 与门店不匹配(到店应为 1/1,得来速 1/5);
- 门店当前不支持堂食/外带。

用 `order preview` 会打印**可选取餐方式列表**,可用 `--take-way eat-in` 或
`--take-way take-in-store` 显式指定。

### 9.3 报错"外送/团餐场景必须提供 addressId"

`StoreContext.address_id` 为空。需要先取配送地址:

```
delivery-query-addresses  →  addressId  →  delivery-query-stores
```

本项目会自动选默认地址,若账号没有地址则需用户先在麦当劳 App 里添加。

### 9.4 报错"无法配送到当前地址"

**实测结论**:门店与配送地址不匹配时,`query-meals` / `calculate-price` / 请求装配
**全部成功**,直到 `create-order` 才失败:

```
无法配送到当前地址,请选择其他地址
```

本项目已经在**预览阶段**用 `delivery-query-stores` 反查该地址的可配送门店并拦住:

```
配送校验    未通过
  ⚠ 门店 1950564 不在该地址的可配送门店列表中,`create-order` 会返回
    「无法配送到当前地址」。该地址可配送的门店:麦当劳无锡云蝠大厦餐厅(1990684)、…
```

**正确动作**:把可配送门店列表给用户,让他选一家,然后用 `--store <可配送门店编码>`
重新生成方案。**不要**用 `--allow-delivery-mismatch` 硬提交 —— 那只会浪费一次请求。

### 9.5 报错"金额超出容差,未下单"

提交前重新核价,若与服务端报价相差超过 **¥1.00** 默认拒绝。这是保护措施。

**正确动作**:把差异告诉用户,请他决定。
只有用户明确同意后,才可加 `--allow-amount-mismatch` —— **不要自作主张**。

### 9.6 下单成功后的信息怎么给用户

`create-order` 返回里有两个关键字段:

| 字段 | 含义 |
|---|---|
| `orderId` | 订单号 |
| `payH5Url` | **支付链接**(形如 `https://m.mcd.cn/mcp/scanToPay?orderId=...`) |
| `orderDetail.expirePayTime` | **支付截止时间**(实测下单后 15 分钟) |
| `orderDetail.orderStatus` | 订单状态(待支付 / 已支付 / 已完成 / 订单已取消) |

`order pay` 会自动打印一张**可直接转发到群里的支付卡片**(可用 `--share card.txt` 落盘):

```
🍟 麦当劳团餐已下单 · 门店名
👥 到店自取
💰 应付 ¥40.00
🧾 订单号 1030296…
⏰ 请在 2026-10-12 18:45:10 前完成支付,逾期订单自动关闭
💳 支付链接 https://m.mcd.cn/mcp/scanToPay?orderId=…
```

**必须把支付链接与截止时间原样给用户**,并说明逾期未支付会自动关闭、不产生扣款。

### 9.7 下单失败怎么排查

看返回的 `error` 字段:

| 字段 | 用途 |
|---|---|
| `message` | 麦当劳给的原因,直接展示给用户,不要改写 |
| `tool` | 通常是 `create-order` |
| `traceId` | **服务端问题追踪号**,反馈给麦当劳时必带 |

### 9.8 取消订单

```bash
python -m mcdteam order cancel --order-id <订单号> --reason-code 1
```

取消原因码:`1`=改主意了 `2`=重复下单 `3`=点错了 `4`=地址电话填错
`5`=送达时间选错 `-1`=其它。取消成功返回 `{"cancelResult": true}`,
随后状态会变为 `订单已取消`。

**取消前必须得到用户确认**,这是有副作用的操作。

---

## 十、绝对不要做的事

- ❌ 不要因为报错就**自己估算价格**填进结果
- ❌ 不要删掉或改写 `warnings` / `relaxations`
- ❌ 不要用其他门店的数据顶替当前门店
- ❌ 不要在未验证的情况下说"下单成功"
- ❌ 不要把 Token 内容写进输出或文件
- ❌ **不要为了"试试看"而下单** —— 没有测试环境
- ❌ **不要替用户设置 `MCD_ALLOW_REAL_ORDER`**
