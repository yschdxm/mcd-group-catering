# MCP 工具链速查(Agent 用)

> 本文件供 Agent 在执行过程中查阅。**不要凭记忆推断工具参数**,以下内容均为实测结论。
> 完整版见仓库根目录的 `MCP_INTEGRATION.md`。

Server 信息:`serverInfo.name = mcd-mcp`,协议 `2025-06-18`,接入地址 `https://mcp.mcd.cn`,
`tools/list` 实际返回 **35 个工具**(官方 README 列 33 个)。

---

## 一、参数链规则(最容易传错的地方)

`orderType` / `beType` / `beCode` 是强绑定的,传错会被服务端以
**`缺少参数`** 静默拒绝,而且它**不会告诉你是哪个参数**。

| 取餐方式 | orderType | beType | beCode | 门店从哪来 |
|---|:---:|:---:|---|---|
| 到店自取(堂食/外带) | 1 | 1 | 不传 | `query-nearby-stores`(beType=1) |
| 得来速车道取餐 | 1 | 5 | **必传** | `query-nearby-stores`(beType=5) |
| 麦乐送到家 | 2 | 2 | **必传** | `delivery-query-stores`(beType=2) |
| 企业团餐 | 2 | 6 | **必传** | `delivery-query-stores`(beType=6) |

**企业团餐额外还需要 `gmServiceCode`**(取自 `query-meal-assistance`),
缺失时 `calculate-price` / `create-order` 会回 `600061 团餐助餐服务参数缺失`。

---

## 二、字段映射:`code` → `productCode`

| 来源工具 | 字段名 |
|---|---|
| `query-meals` 返回的商品标识 | **`code`** |
| `calculate-price` / `create-order` 需要的入参 | **`items[].productCode`** |

两者必须是同一个值。**传错字段不报错,直接返回 `price: 0`。**

---

## 三、按场景的工具调用顺序

### 到店自取 / 得来速

```
query-nearby-stores(beType=1或5, searchType=1或2, city?, keyword?)
   └─► storeCode (+ 得来速的 beCode)
        ├─► query-meals(storeCode, orderType=1, beType=1或5, beCode?)
        ├─► query-store-coupons(同上)
        └─► calculate-price(同上 + items[])
```

### 麦乐送 / 企业团餐

```
delivery-query-addresses()                    → addressId
   └─► delivery-query-stores(addressId, beType=2或6)
        └─► storeCode + beCode
             ├─► [团餐] query-meal-assistance(storeCode, orderType=2, beType=6, beCode)
             │      └─► gmServiceCode(取 selected=true 且 enable!=false 的那项)
             ├─► query-meals(...)  ← 团餐场景即便没有 gmServiceCode 也能返回菜单
             ├─► query-store-coupons(...)
             └─► calculate-price(... + gmServiceCode)
```

> ⚠️ 注意 `query-meals` 在缺 `gmServiceCode` 时**仍会成功返回菜单**,
> 所以这个参数漏了只会在取价阶段爆炸。这也是本 Skill 把「取价全军覆没」
> 处理成显式错误而不是空结果的原因。

### 营养

```
list-nutrition-foods()                        → 160 条单品营养(文本表格,不是 JSON)
query-meal-detail(storeCode, orderType, beType, code)
   └─► rounds[].choices[] 中 quantity>0 的默认组成 → 按名称匹配营养表后累加
```

---

## 四、九个实测坑(避免重复踩)

1. **请求体必须是 UTF-8 字节流。** 用某些运行时默认编码发送会被判定为
   `缺少参数`。本项目客户端统一 `json.dumps(..., ensure_ascii=False).encode("utf-8")`。

2. **门店级工具必须传有效 storeCode。** 传格式正确但不存在的编码,返回的也是
   `缺少参数`,而不是「门店不存在」。

3. **`list-nutrition-foods` 返回文本表格**,首行形如
   `[160]{productName,nutritionDescription,...}:`;
   `nutritionDescription` 可能是 `null`,也可能**含逗号**,不能按下标硬切;
   存在重复行;该接口**偶发返回空内容**。

4. **一次性传全部商品编码会被整批拒绝。** 实测 117 个编码一次传入 → 0 个成功;
   6 个一批 + 失败逐条重试 → 116/117(99.1%)。

5. **门店实价 ≠ 菜单快照价。** 实测同一清单本地估算 ¥214.50、门店实时 ¥231.70。
   任何金额都必须经 `calculate-price` 回算。

6. **同一餐品可能有多个编码。** 实测「鱼排堡开心乐园餐」「汉堡开心乐园餐」各有
   两个 spu 编码,需按菜名去重。

7. **官方营养数据不覆盖套餐。** 只覆盖单品;套餐需用 `query-meal-detail`
   拆解默认组成后按子项名称累加。

8. **团餐必须有 `gmServiceCode`**(见第一节)。

9. **同一编码在不同接口可能返回不同名称。** 实测编码 `9900015602`:
   `calculate-price` 叫「牛肉堡单品精选」,`query-meal-detail` 是
   「大堡口福三件套」。名称明显不符时**不要**套用营养,以门店实际商品为准。

---

## 五、限流与错误码

| HTTP | 含义 | 处理 |
|:---:|---|---|
| 401 | Token 无效、过期或未提供 | 提示用户重新获取 Token |
| 429 | 超过 600 次/分钟 | 客户端已内置滑动窗口限速与指数退避,无需人工干预 |

**注意:业务失败不一定带 HTTP 错误码。** 必须检查响应里的
`structuredContent.success`;`success=false` 时 `message` 才是真实原因。
把「HTTP 200」当成成功是本项目踩过的最隐蔽的坑。
