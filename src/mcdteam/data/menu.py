"""菜单模型 : 把 MCP 的三份原料拼成一张「可计算的菜单」。

原料来源与已知局限:

============================  ==========================================
``query-meals``               只给 ``code`` + ``tags`` + 分类名,**没有菜名、没有价格**
``calculate-price``           给菜名与单价(传入 code 列表即返回 productList)
``list-nutrition-foods``      给 160 条单品营养,覆盖率约 60%~70%
============================  ==========================================

因此本项目引入 :class:`MenuCatalog`,由 ``query-meals`` 的 code 集合驱动,
用一次 ``calculate-price`` 批量拿到菜名+价格,再用营养书名匹配补上营养。
菜名到手后再由 :func:`classify` 推断「主食 / 小食 / 饮料 / 甜品」角色 —— 这份
角色推断是团餐自动搭配的前提,因为 MCP 不提供单品维度的分类字段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .nutrition import Nutrition, NutritionBook, normalize_name
from ..mcp.errors import MCPBusinessError


class Role(str, Enum):
    """搭配角色。团餐方案必须覆盖主食用量,再按预算补小食/饮料。"""

    MAIN = "main"        # 汉堡 / 卷 / 饭 等主食
    SIDE = "side"        # 薯条 / 鸡块 / 鸡翅 等小食
    DRINK = "drink"      # 饮料 / 咖啡 / 牛奶
    DESSERT = "dessert"  # 冰淇淋 / 派 / 麦旋风
    BREAKFAST = "breakfast"  # 早餐麦满分等
    COMBO = "combo"      # 套餐(含多件,通常不可直接按人分)
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            Role.MAIN: "主食", Role.SIDE: "小食", Role.DRINK: "饮料",
            Role.DESSERT: "甜品", Role.BREAKFAST: "早餐",
            Role.COMBO: "套餐", Role.OTHER: "其他",
        }[self]


#: 角色关键词表(先匹配先返回,顺序即优先级)
_ROLE_RULES: List[tuple] = [
    (Role.COMBO, ("随心配", "小食拼", "多人餐", "双人餐", "套餐", "拼盘", "桶",
                  "组合", "family", "分享")),
    (Role.BREAKFAST, ("麦满分", "厚松饼", "早安", "营养卷", "粥", "脆薯饼", "油条")),
    (Role.DESSERT, ("冰淇淋", "圆筒", "新地", "麦旋风", "派", "拉明顿", "阿芙佳朵",
                    "奶冻", "爆珠", "雪冰")),
    # 瓶装水/纯悦这类「无热量饮品」也归饮料,否则会被当成主食占用配额
    (Role.DRINK, ("可乐", "雪碧", "咖啡", "美式", "奶铁", "拿铁", "卡布奇诺",
                  "玛奇朵", "红茶", "绿茶", "牛奶", "豆浆", "纯悦", "矿泉水",
                  "橙汁", "苹果汁", "柠檬", "朱古力", "怡泉", "阳光橙", "燕麦",
                  "抹茶", "黑巧", "奶昔", "果汁")),
    # 炸鸡/鸡翅/鸡球属于可分食小食,不能占主食配额(实测踩坑点)
    (Role.SIDE, ("麦乐鸡", "鸡翅", "鸡块", "鸡球", "V翅", "V 翅", "脆汁鸡",
                 "薯条", "薯饼", "薯角", "玉米杯", "苹果片", "沙拉", "蘸酱",
                 "香肠", "鸡排")),
    (Role.MAIN, ("汉堡", "堡", "巨无霸", "麦香鸡", "麦香鱼", "麦辣", "板烧",
                 "卷", "叠叠卷", "果然多肉", "薄皮", "脆汁")),
]

#: 辣度关键词(用于「不吃辣」硬约束过滤)
_SPICY_WORDS = ("辣", "川", "椒", "藤椒", "香辣", "麻辣", "辣椒")
#: 牛肉关键词(用于「不吃牛肉」硬约束过滤)
_BEEF_WORDS = ("牛", "巨无霸", "安格斯", "吉士汉堡", "汉堡包", "培根")
#: 含猪肉关键词
_PORK_WORDS = ("猪柳", "猪", "培根", "火腿", "香肠", "图林根")
#: 鱼类/海鲜关键词
_FISH_WORDS = ("鱼", "鳕鱼", "虾", "海鲜")
#: 含鸡肉关键词
_CHICKEN_WORDS = ("鸡", "翅", "麦乐鸡", "板烧", "脆汁", "V翅")
#: 含蛋关键词
_EGG_WORDS = ("蛋", "炒双蛋")
#: 乳制品关键词(乳糖不耐受)
_DAIRY_WORDS = ("奶", "芝士", "吉士", "冰淇淋", "新地", "麦旋风", "拿铁",
                "卡布奇诺", "玛奇朵", "阿芙佳朵", "奶冻")

_SIZE_HINTS = [
    ("迷你", 0.5), ("小杯", 0.6), ("小", 0.7), ("1块", 0.2), ("2块", 0.35),
    ("4块", 0.6), ("5块", 0.75), ("中杯", 1.0), ("大杯", 1.35), ("大", 1.3),
    ("双层", 1.35), ("超大", 1.5),
]

#: 蘸酱、调味酱、餐具等「非食物」条目。它们价格极低,若不排除,
#: 求解器会把剩余预算全换成几十包酱料(实测踩坑点)。
CONDIMENT_WORDS = ("酱", "蘸", "番茄沙司", "餐具", "纸巾", "糖包", "奶精",
                   "调味", "料包")


def classify(name: str, *, category: str = "", tags: Sequence[str] = ()) -> Role:
    """按名称 + 分类名 + 标签推断角色。

    这是纯启发式函数,故意保持「可解释、可单测」:所有规则都写在
    :data:`_ROLE_RULES` 里,新增品类只需加一行。
    """
    haystack = f"{name} {category} {' '.join(tags or [])}"
    for role, words in _ROLE_RULES:
        for word in words:
            if word in haystack:
                return role
    return Role.OTHER


def spice_level(name: str) -> int:
    """0 = 不辣,1 = 可能微辣,2 = 明确辣。"""
    text = name or ""
    if any(w in text for w in ("香辣", "麻辣", "藤椒", "辣椒", "剁椒")):
        return 2
    if any(w in text for w in _SPICY_WORDS):
        return 1
    if "板烧" in text or "原味" in text:
        return 0
    return 0


def portion_factor(name: str) -> float:
    """从名称推断份量系数(相对 1 人份主食/中杯饮料)。"""
    text = name or ""
    for hint, factor in _SIZE_HINTS:
        if hint in text:
            return factor
    return 1.0


@dataclass
class MenuItem:
    """一条可下单的餐品。"""

    code: str
    name: str
    price: int = 0                     # 单位:分
    role: Role = Role.OTHER
    category: str = ""
    tags: List[str] = field(default_factory=list)
    nutrition: Optional[Nutrition] = None
    is_combo: bool = False
    sold_out: bool = False
    #: 营养是否由套餐组成累加得出(而非官方营养表直接命中)
    nutrition_derived: bool = False
    #: 同一餐品在菜单里可能对应多个编码(实测:开心乐园餐有两个 spu 编码),
    #: 这里保留被合并掉的编码,报告里会提示,避免用户以为漏了东西
    aliases: List[str] = field(default_factory=list)

    @property
    def price_yuan(self) -> float:
        return round(self.price / 100.0, 2)

    @property
    def kcal(self) -> Optional[float]:
        return self.nutrition.energy_kcal if self.nutrition else None

    @property
    def protein(self) -> Optional[float]:
        return self.nutrition.protein if self.nutrition else None

    @property
    def sodium(self) -> Optional[float]:
        return self.nutrition.sodium if self.nutrition else None

    @property
    def spice(self) -> int:
        return spice_level(self.name)

    @property
    def contains_beef(self) -> bool:
        return any(w in (self.name or "") for w in _BEEF_WORDS)

    @property
    def contains_pork(self) -> bool:
        return any(w in (self.name or "") for w in _PORK_WORDS)

    @property
    def contains_fish(self) -> bool:
        return any(w in (self.name or "") for w in _FISH_WORDS)

    @property
    def contains_chicken(self) -> bool:
        return any(w in (self.name or "") for w in _CHICKEN_WORDS)

    @property
    def contains_egg(self) -> bool:
        return any(w in (self.name or "") for w in _EGG_WORDS)

    @property
    def contains_dairy(self) -> bool:
        return any(w in (self.name or "") for w in _DAIRY_WORDS)

    @property
    def is_condiment(self) -> bool:
        """是否为蘸酱/调味料等非主食类条目。"""
        return any(word in (self.name or "") for word in CONDIMENT_WORDS)

    @property
    def is_vegetarian(self) -> bool:
        """粗判素食:不含各类肉/鱼,且不是炸鸡类小食。"""
        return not (self.contains_beef or self.contains_pork or self.contains_fish
                    or self.contains_chicken)

    def protein_per_yuan(self) -> float:
        """性价比指标:每元蛋白质克数。团餐预算紧张时的主要排序依据。"""
        if not self.price or self.protein is None:
            return 0.0
        return round(self.protein / self.price_yuan, 3)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "price": self.price,
            "priceYuan": self.price_yuan,
            "role": self.role.value,
            "roleLabel": self.role.label,
            "category": self.category,
            "tags": list(self.tags),
            "isCombo": self.is_combo,
            "soldOut": self.sold_out,
            "nutritionDerived": self.nutrition_derived,
            "aliases": list(self.aliases),
            "nutrition": self.nutrition.to_dict() if self.nutrition else None,
            "flags": {
                "spice": self.spice,
                "beef": self.contains_beef,
                "pork": self.contains_pork,
                "fish": self.contains_fish,
                "chicken": self.contains_chicken,
                "egg": self.contains_egg,
                "dairy": self.contains_dairy,
                "vegetarian": self.is_vegetarian,
            },
        }


class MenuCatalog:
    """门店菜单索引。"""

    def __init__(self, items: Iterable[MenuItem], *, store_code: str = "",
                 store_name: str = "", source: str = "live") -> None:
        self.items: List[MenuItem] = [i for i in items if i.code]
        self.store_code = store_code
        self.store_name = store_name
        self.source = source
        self._by_code: Dict[str, MenuItem] = {i.code: i for i in self.items}

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def get(self, code: str) -> Optional[MenuItem]:
        return self._by_code.get(str(code))

    def by_role(self, role: Role, *, exclude_combo: bool = True) -> List[MenuItem]:
        return [i for i in self.items
                if i.role is role and not (exclude_combo and i.is_combo) and not i.sold_out]

    def priced(self) -> List[MenuItem]:
        return [i for i in self.items if i.price > 0 and not i.sold_out]

    def categories(self) -> List[str]:
        seen, out = set(), []
        for item in self.items:
            if item.category and item.category not in seen:
                seen.add(item.category)
                out.append(item.category)
        return out

    def stats(self) -> Dict[str, Any]:
        roles: Dict[str, int] = {}
        for item in self.items:
            roles[item.role.label] = roles.get(item.role.label, 0) + 1
        with_nutrition = sum(1 for i in self.items if i.nutrition)
        return {
            "storeCode": self.store_code,
            "storeName": self.store_name,
            "source": self.source,
            "total": len(self.items),
            "priced": len(self.priced()),
            "withNutrition": with_nutrition,
            "nutritionRate": round(with_nutrition / (len(self.items) or 1), 4),
            "roles": roles,
            "priceRange": [
                min((i.price_yuan for i in self.priced()), default=0.0),
                max((i.price_yuan for i in self.priced()), default=0.0),
            ],
        }

    def to_dict(self) -> Dict[str, Any]:
        return {"stats": self.stats(), "items": [i.to_dict() for i in self.items]}


def build_catalog(meals_payload: Dict[str, Any],
                  price_map: Dict[str, Dict[str, Any]],
                  nutrition_book: Optional[NutritionBook] = None,
                  *,
                  store_code: str = "", store_name: str = "",
                  source: str = "live") -> MenuCatalog:
    """把 ``query-meals`` + 价格表 + 营养书拼成 :class:`MenuCatalog`。

    Parameters
    ----------
    meals_payload:
        ``query-meals`` 的 ``data``(含 ``categories[].meals[].code`` 与 ``tags``)。
    price_map:
        ``{code: {"productName": str, "subtotal": int}}``,
        由 :func:`collect_price_map` 批量探测得到。缺失的 code 视为售罄。
    """
    category_of: Dict[str, str] = {}
    tags_of: Dict[str, List[str]] = {}
    order: List[str] = []
    for group in (meals_payload or {}).get("categories", []) or []:
        category_name = str(group.get("name", "") or "").replace("\n", "")
        for meal in group.get("meals", []) or []:
            code = str(meal.get("code", "") or "")
            if not code:
                continue
            if code not in category_of:
                category_of[code] = category_name
                order.append(code)
            tags_of.setdefault(code, [])
            for tag in meal.get("tags", []) or []:
                if tag not in tags_of[code]:
                    tags_of[code].append(str(tag))

    items: List[MenuItem] = []
    merged: Dict[str, MenuItem] = {}
    for code in order:
        record = price_map.get(code) or {}
        name = str(record.get("productName") or "").strip()
        sold_out = not record
        if not name:
            # 价格接口查不到该 code:保留占位,标记售罄,避免静默丢数据
            name = f"[未在售] {code}"
        price = int(record.get("subtotal") or record.get("price") or 0)
        category = category_of.get(code, "")
        tags = tags_of.get(code, [])

        key = normalize_name(name)
        existing = merged.get(key) if key and not sold_out else None
        if existing is not None:
            # 同一餐品的第二个编码:合并到已有条目,价格取更低的那个
            existing.aliases.append(existing.code)
            if price and (not existing.price or price < existing.price):
                existing.price = price
                existing.code = code
            if code not in existing.aliases:
                existing.aliases.append(code)
            for tag in tags:
                if tag not in existing.tags:
                    existing.tags.append(tag)
            for extra_category in (category,):
                if extra_category and extra_category not in existing.category:
                    existing.category = f"{existing.category}/{extra_category}".strip("/")
            continue

        role = classify(name, category=category, tags=tags)
        nutrition = nutrition_book.match(name) if nutrition_book else None
        item = MenuItem(
            code=code,
            name=name,
            price=price,
            role=role,
            category=category,
            tags=tags,
            nutrition=nutrition,
            is_combo=role is Role.COMBO or "套餐" in tags or "拼盘" in tags,
            sold_out=sold_out,
        )
        items.append(item)
        if key:
            merged[key] = item

    return MenuCatalog(items, store_code=store_code, store_name=store_name, source=source)


def collect_price_map(client, store_context, codes: Sequence[str], *,
                      batch_size: int = 6, progress=None,
                      raise_on_total_failure: bool = True) -> Dict[str, Dict[str, Any]]:
    """逐个批次用 ``calculate-price`` 反查菜名与单价。

    为什么要分批:实测把 117 个 code 一次性传入时服务端直接返回
    ``商品已售罄或不可售``,整批失败;而 6 个一批时失败率极低,且失败的
    批次可以逐条重试定位到具体某个下架商品。这一策略把菜单覆盖率从
    0% 提升到 **99.1%**(117 个 code 中 116 个成功取价)。

    ``raise_on_total_failure``:当一个都没取到时抛出明确异常。**不要静默返回空表** ——
    实测团餐场景漏传 ``gmServiceCode`` 时,``calculate-price`` 全部返回
    ``success=false``,若静默处理就会生成一份空方案,用户看到的是「合计 ¥0.00」
    这种毫无信息量的结果。
    """
    price_map: Dict[str, Dict[str, Any]] = {}
    unique = [c for c in dict.fromkeys(str(c) for c in codes) if c]
    failed_batches: List[List[str]] = []
    last_error: str = ""

    for start in range(0, len(unique), max(1, batch_size)):
        batch = unique[start:start + max(1, batch_size)]
        if progress:
            progress(f"取价 {start + 1}-{start + len(batch)}/{len(unique)}")
        try:
            data = client.calculate_price(
                store_context.store_code,
                [{"productCode": code, "quantity": 1} for code in batch],
                order_type=store_context.order_type,
                be_type=store_context.be_type,
                be_code=store_context.be_code,
                reservation_date=store_context.reservation_date,
                gm_service_code=getattr(store_context, "gm_service_code", ""),
            )
        except Exception as exc:  # noqa: BLE001 - 批失败不影响整体,退化为逐条
            last_error = str(exc)
            failed_batches.append(batch)
            continue
        for product in data.get("productList", []) or []:
            code = str(product.get("productCode", ""))
            if code:
                price_map[code] = {
                    "productName": product.get("productName", ""),
                    "subtotal": int(product.get("subtotal") or product.get("originalSubtotal") or 0),
                }

    # 逐条重试失败的批次,精确定位下架商品
    for batch in failed_batches:
        for code in batch:
            try:
                data = client.calculate_price(
                    store_context.store_code,
                    [{"productCode": code, "quantity": 1}],
                    order_type=store_context.order_type,
                    be_type=store_context.be_type,
                    be_code=store_context.be_code,
                    gm_service_code=getattr(store_context, "gm_service_code", ""),
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                continue
            for product in data.get("productList", []) or []:
                found = str(product.get("productCode", ""))
                if found:
                    price_map[found] = {
                        "productName": product.get("productName", ""),
                        "subtotal": int(product.get("subtotal") or 0),
                    }

    if unique and not price_map and raise_on_total_failure:
        raise MCPBusinessError(
            "全部门店餐品取价失败" + (f",服务端最后一次回复:{last_error}" if last_error else "")
            + "。请检查 storeCode / orderType / beType / beCode 是否匹配"
            +(f";企业团餐还需要 gmServiceCode(当前为 "
              f"{getattr(store_context, 'gm_service_code', '') or '空'})" 
              if getattr(store_context, "be_type", 0) == 6 else ""),
            tool="calculate-price",
        )
    return price_map
