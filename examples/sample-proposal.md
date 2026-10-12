# 团餐采购提案 · 1950564

> 生成时间:2026-10-12 17:40:12　数据来源:离线快照兜底:list-nutrition-foods, query-meals, query-store-coupons

## 一、需求与理解

| 项目 | 内容 |
|---|---|
| 人数 | 14 人 |
| 预算 | ¥480.00(人均 ¥34.29) |
| 取餐方式 | 麦乐送 |
| 期望时间 | 2026-10-13 12:00 |
| 用餐时段 | lunch |
| 硬性忌口 | 不吃辣 |
| 偏好 | 低卡优先、需要可分食的小食/拼盘 |

## 二、采购清单

| # | 餐品 | 角色 | 单价 | 数量 | 小计 | 单件热量 |
|---:|---|---|---:|---:|---:|---:|
| 1 | 培根蔬萃双层牛堡 | 主食 | ¥24.50 | 1 | ¥24.50 | 442 kcal |
| 2 | 双层深海鳕鱼堡 | 主食 | ¥24.00 | 2 | ¥48.00 | 485 kcal |
| 3 | 麦香鱼 | 主食 | ¥22.50 | 1 | ¥22.50 | 325 kcal |
| 4 | 麦香鸡 | 主食 | ¥17.00 | 3 | ¥51.00 | 369 kcal |
| 5 | 薄皮焦香V翅 | 小食 | ¥16.00 | 4 | ¥64.00 | 192 kcal |
| 6 | 牛肉堡单品精选 | 主食 | ¥14.00 | 7 | ¥98.00 | — |
| 7 | 小杯玉米杯 | 小食 | ¥13.00 | 1 | ¥13.00 | 53 kcal |
| 8 | 锡兰红茶 | 饮料 | ¥11.50 | 2 | ¥23.00 | 2 kcal |
| 9 | 小杯鲜萃咖啡 | 饮料 | ¥9.50 | 6 | ¥57.00 | 15 kcal |
| 10 | 雪碧小杯 | 饮料 | ¥9.50 | 4 | ¥38.00 | 76 kcal |
| 11 | 苹果片一袋(60g) | 小食 | ¥7.00 | 4 | ¥28.00 | 32 kcal |
| 12 | 圆筒冰淇淋 | 甜品 | ¥5.00 | 1 | ¥5.00 | 93 kcal |
| | **合计** | | | **36** | **¥472.00** | |

## 三、费用

| 项目 | 金额 |
|---|---:|
| 商品原价 | ¥472.00 |
| **应付总额** | **¥472.00** |
| 人均 | ¥33.71 |
| 预算余量 | ¥8.00 |

## 四、营养参考

| 指标 | 整单 | 人均 |
|---|---:|---:|
| 热量 | 4,284 kcal | 306 kcal |
| 蛋白质 | 219 g | 16 g |
| 脂肪 | 177 g | 13 g |
| 碳水 | 445 g | 32 g |
| 钠 | 7,820 mg | 559 mg |

> 注:7 件餐品未匹配到官方营养数据,上表为下限值。营养素数据来自麦当劳 MCP `list-nutrition-foods`。

## 五、需要确认的事项

- 有 7 件餐品缺少官方营养数据,营养合计为下限值而非精确值

## 附:调用轨迹

```json
{
  "generatedAt": "2026-10-12 17:40:12",
  "config": {
    "endpoint": "https://mcp.mcd.cn",
    "token": "(未设置)",
    "storeCode": "1950564",
    "city": null,
    "beCode": null,
    "offline": true,
    "fixtureDir": "F:\\dsh\\MC\\mcd-group-catering\\fixtures"
  },
  "storeContext": {
    "storeCode": "1950564",
    "storeName": "",
    "mode": "delivery",
    "orderType": 2,
    "beType": 2,
    "beCode": "offline",
    "reservationDate": "2026-10-13 12:00",
    "addressId": null,
    "city": null
  },
  "calls": [],
  "snapshots": [
    "query-meals <- F:\\dsh\\MC\\mcd-group-catering\\fixtures\\query-meals.1950564.json",
    "list-nutrition-foods <- F:\\dsh\\MC\\mcd-group-catering\\fixtures\\nutrition_raw.txt",
    "query-store-coupons <- F:\\dsh\\MC\\mcd-group-catering\\fixtures\\query-store-coupons.1950564.json"
  ],
  "steps": [
    {
      "step": "resolve_store",
      "note": "离线模式使用占位 beCode"
    },
    {
      "step": "resolve_store",
      "storeCode": "1950564",
      "source": "explicit"
    },
    {
      "step": "query-meals",
      "storeCode": "1950564",
      "codes": 117
    },
    {
      "step": "price-snapshot-mismatch",
      "capturedOrderType": 1,
      "requestedOrderType": 2,
      "note": "快照价格来自不同的点餐方式,金额仅供演示,请以实时接口为准"
    },
    {
      "step": "calculate-price",
      "requested": 117,
      "priced": 116
    },
    {
      "step": "list-nutrition-foods",
      "source": "snapshot"
    },
    {
      "step": "query-store-coupons",
      "coupons": 2
    },
    {
      "step": "plan",
      "payableYuan": 472.0,
      "items": 36,
      "underBudget": true
    }
  ]
}
```

---

本提案由「麦麦团餐统筹官」(第三方 Skill,非麦当劳官方产品)基于麦当劳 MCP 实时数据生成;餐品价格、可售状态以门店实时结果为准,营养信息仅供参考,不构成医疗或营养建议。
