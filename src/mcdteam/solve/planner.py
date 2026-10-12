"""团餐统筹求解器。

问题定义
--------
给定一家门店的可售菜单、一组硬性忌口、一个总预算和一批人数配额,
选出「够吃、不超预算、不踩忌口、尽量便宜」的一份采购清单。

形式化::

    minimize    cost(plan) + λ·deficit(plan) + μ·softpenalty(plan)
    subject to  cost(plan) ≤ budget
                plan 中每件餐品都不违反任一硬性忌口
                plan 满足 role_quota 的件数下限

求解策略
--------
团餐配比是一个带基数约束的多选择背包问题,精确求解需要 ILP。本项目坚持
**零第三方依赖**(评审只需 ``python -m`` 即可跑通),因此采用两步法:

1. **可行性过滤**:按硬性忌口 + 营养上限剔除不可用餐品。若剔除后主食清空,
   则按「辣 → 蛋 → 乳 → 鱼 → 猪 → 牛 → 鸡」的宽松顺序逐条让位,并把每一次
   让位如实写进报告的 ``relaxations`` —— 宁可少满足一条约束,也不能给出
   违反用户明确声明的方案。
2. **随机多起点局部搜索**:每个起点按配额随机装配,随后反复尝试
   「越界修正 → 换更便宜的同类 → 在预算内加件」。目标函数是
   ``成本(分) + 缺口罚分 + 软偏好罚分``,分数低者胜;重启 ``effort`` 次取最优。

最后用 ``calculate-price`` 回算真实总价,避免「按单价相加」与服务端促销/券
计算口径不一致。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..data.coupons import Coupon, Promotion
from ..data.menu import MenuCatalog, MenuItem, Role
from .constraints import CateringRequirements, role_quota

ProgressFn = Optional[Callable[[str], None]]

#: 硬性约束名 -> 判定函数。用于过滤与「让位」时的解释输出
HARD_RULES: List[Tuple[str, str, Callable[[MenuItem], bool]]] = [
    ("avoid_spicy", "不吃辣", lambda i: i.spice == 0),
    ("avoid_beef", "不吃牛肉", lambda i: not i.contains_beef),
    ("avoid_pork", "不吃猪肉", lambda i: not i.contains_pork),
    ("avoid_fish", "不吃鱼类海鲜", lambda i: not i.contains_fish),
    ("avoid_chicken", "不吃鸡肉", lambda i: not i.contains_chicken),
    ("avoid_egg", "不吃蛋", lambda i: not i.contains_egg),
    ("avoid_dairy", "乳制品不耐受", lambda i: not i.contains_dairy),
    ("vegetarian", "素食", lambda i: i.is_vegetarian),
]

#: 让位顺序:越靠后越「贵」,即越不愿意放宽
RELAX_ORDER = ["avoid_spicy", "avoid_egg", "avoid_dairy", "avoid_fish",
               "avoid_pork", "avoid_beef", "avoid_chicken", "vegetarian"]

#: 角色 -> 缺一件的罚分(分)。主食最重,甜品最轻
DEFICIT_PENALTY = {
    Role.MAIN: 6000, Role.SIDE: 1200, Role.DRINK: 400,
    Role.DESSERT: 300, Role.BREAKFAST: 3000, Role.COMBO: 3000, Role.OTHER: 600,
}

#: 单一餐品在同角色内的数量上限(相对配额的比例),用于保证多样性
VARIETY_CAP = {Role.MAIN: 0.5, Role.SIDE: 0.6, Role.DRINK: 0.6, Role.DESSERT: 0.6}

#: 瓶装饮用水不占用饮料配额(会议订餐时通常是加购,不该把预算耗在水上)
WATER_WORDS = ("纯悦", "矿泉水", "饮用水", "纯净水")


@dataclass
class PlanLine:
    """方案中的一行。"""

    item: MenuItem
    quantity: int = 1

    @property
    def subtotal(self) -> int:
        return self.item.price * self.quantity

    def to_dict(self) -> Dict[str, Any]:
        return {
            "productCode": self.item.code,
            "productName": self.item.name,
            "role": self.item.role.value,
            "roleLabel": self.item.role.label,
            "quantity": self.quantity,
            "unitPrice": self.item.price,
            "unitPriceYuan": self.item.price_yuan,
            "subtotal": self.subtotal,
            "nutrition": self.item.nutrition.to_dict() if self.item.nutrition else None,
        }


@dataclass
class CateringPlan:
    """求解结果。"""

    store_code: str = ""
    store_name: str = ""
    headcount: int = 0
    lines: List[PlanLine] = field(default_factory=list)
    budget_cents: int = 0
    subtotal_cents: int = 0
    discount_cents: int = 0
    delivery_fee_cents: int = 0
    payable_cents: int = 0
    coupons_applied: List[Coupon] = field(default_factory=list)
    promotions_applied: List[Promotion] = field(default_factory=list)
    relaxations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    #: 与 ``warnings`` 一一对应的分类标签,用于覆盖式更新
    _warning_codes: List[str] = field(default_factory=list, repr=False)
    score: float = 0.0
    verified_by_api: bool = False
    #: 回算前的本地估算原价(用于报告里说明差异)
    local_estimate_cents: int = 0
    api_trace: List[str] = field(default_factory=list)
    total_quantity: int = 0
    _nutrition_totals: Dict[str, float] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ 汇总
    def recalc(self) -> None:
        self.subtotal_cents = sum(line.subtotal for line in self.lines)
        self.total_quantity = sum(line.quantity for line in self.lines)
        if self.payable_cents <= 0:
            self.payable_cents = max(0, self.subtotal_cents - self.discount_cents)
        if self.discount_cents == 0:
            self.discount_cents = max(0, self.subtotal_cents - self.payable_cents)
        self._nutrition_totals = {}

    @property
    def payable_yuan(self) -> float:
        return round(self.payable_cents / 100.0, 2)

    @property
    def subtotal_yuan(self) -> float:
        return round(self.subtotal_cents / 100.0, 2)

    @property
    def discount_yuan(self) -> float:
        return round(self.discount_cents / 100.0, 2)

    @property
    def per_person_cents(self) -> float:
        return self.payable_cents / max(1, self.headcount)

    @property
    def per_person_yuan(self) -> float:
        return round(self.per_person_cents / 100.0, 2)

    @property
    def budget_delta_yuan(self) -> float:
        return round((self.budget_cents - self.payable_cents) / 100.0, 2)

    @property
    def under_budget(self) -> bool:
        return self.budget_cents == 0 or self.payable_cents <= self.budget_cents

    def nutrition_totals(self) -> Dict[str, float]:
        """整单营养合计。缺营养数据的行按「未计入」处理,不按 0 累计。"""
        if self._nutrition_totals:
            return self._nutrition_totals
        totals = {"kcal": 0.0, "protein": 0.0, "fat": 0.0,
                  "carbohydrate": 0.0, "sodium": 0.0, "calcium": 0.0}
        counted = 0
        for line in self.lines:
            nutrition = line.item.nutrition
            if not nutrition:
                continue
            counted += line.quantity
            totals["kcal"] += (nutrition.energy_kcal or 0.0) * line.quantity
            totals["protein"] += (nutrition.protein or 0.0) * line.quantity
            totals["fat"] += (nutrition.fat or 0.0) * line.quantity
            totals["carbohydrate"] += (nutrition.carbohydrate or 0.0) * line.quantity
            totals["sodium"] += (nutrition.sodium or 0.0) * line.quantity
            totals["calcium"] += (nutrition.calcium or 0.0) * line.quantity
        totals["_counted_quantity"] = float(counted)
        self._nutrition_totals = totals
        return totals

    def per_person(self, key: str) -> float:
        return round(self.nutrition_totals().get(key, 0.0) / max(1, self.headcount), 1)

    def role_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for line in self.lines:
            counts[line.item.role.label] = counts.get(line.item.role.label, 0) + line.quantity
        return counts

    # ------------------------------------------------------------ 警告管理
    def warn(self, code: str, message: str) -> None:
        """登记一条带分类的提示,便于被后续轮次覆盖而不是层层堆积。"""
        self.warnings.append(message)
        self._warning_codes.append(code)

    def clear_warnings(self, code: str) -> None:
        """清掉某一类提示(例如自动收敛后「超预算」提示必须失效)。"""
        kept: List[str] = []
        kept_codes: List[str] = []
        for index, existing in enumerate(self.warnings):
            existing_code = (self._warning_codes[index]
                             if index < len(self._warning_codes) else "")
            if existing_code == code:
                continue
            kept.append(existing)
            kept_codes.append(existing_code)
        self.warnings = kept
        self._warning_codes = kept_codes

    def to_dict(self) -> Dict[str, Any]:
        totals = self.nutrition_totals()
        return {
            "storeCode": self.store_code,
            "storeName": self.store_name,
            "headcount": self.headcount,
            "budgetYuan": round(self.budget_cents / 100.0, 2),
            "subtotalYuan": self.subtotal_yuan,
            "localEstimateYuan": (round(self.local_estimate_cents / 100.0, 2)
                                  if self.local_estimate_cents else None),
            "discountYuan": self.discount_yuan,
            "deliveryFeeYuan": round(self.delivery_fee_cents / 100.0, 2),
            "payableYuan": self.payable_yuan,
            "perPersonYuan": self.per_person_yuan,
            "underBudget": self.under_budget,
            "totalQuantity": self.total_quantity,
            "roleCounts": self.role_counts(),
            "verifiedByApi": self.verified_by_api,
            "relaxations": list(self.relaxations),
            "warnings": list(self.warnings),
            "couponsApplied": [c.to_dict() for c in self.coupons_applied],
            "promotionsApplied": [p.to_dict() for p in self.promotions_applied],
            "items": [line.to_dict() for line in self.lines],
            "nutrition": {
                "total": {k: round(v, 1) for k, v in totals.items() if not k.startswith("_")},
                "perPerson": {
                    "kcal": self.per_person("kcal"),
                    "protein": self.per_person("protein"),
                    "fat": self.per_person("fat"),
                    "carbohydrate": self.per_person("carbohydrate"),
                    "sodium": self.per_person("sodium"),
                },
                "countedQuantity": int(totals.get("_counted_quantity", 0)),
            },
        }


# --------------------------------------------------------------------- 过滤
@dataclass
class CandidatePool:
    """通过硬性过滤的候选池。"""

    by_role: Dict[Role, List[MenuItem]] = field(default_factory=dict)
    excluded: Dict[str, List[str]] = field(default_factory=dict)
    relaxations: List[str] = field(default_factory=list)

    def all_items(self) -> List[MenuItem]:
        out: List[MenuItem] = []
        for items in self.by_role.values():
            out.extend(items)
        return out

    def pick(self, role: Role) -> List[MenuItem]:
        return self.by_role.get(role, [])


def build_candidate_pool(catalog: MenuCatalog, requirements: CateringRequirements,
                         *, progress: ProgressFn = None) -> CandidatePool:
    """按硬性规则过滤菜单;主食池为空时按 :data:`RELAX_ORDER` 逐条让位。"""
    active_rules: List[Tuple[str, str, Callable[[MenuItem], bool]]] = [
        (name, label, predicate) for name, label, predicate in HARD_RULES
        if getattr(requirements, name, False)
    ]
    relaxations: List[str] = []
    excluded: Dict[str, List[str]] = {}

    def filter_items(rules) -> List[MenuItem]:
        out = []
        for item in catalog.priced():
            if item.code in requirements.excluded_codes:
                excluded.setdefault(item.name, []).append("用户指定排除")
                continue
            if item.is_condiment:
                excluded.setdefault(item.name, []).append("蘸酱/调味料,不计入餐食配额")
                continue
            if any(word in item.name for word in WATER_WORDS):
                excluded.setdefault(item.name, []).append("饮用水不占饮料配额")
                continue
            violations = [label for _, label, predicate in rules if not predicate(item)]
            if violations:
                excluded.setdefault(item.name, []).extend(violations)
                continue
            out.append(item)
        return out

    kept = filter_items(active_rules)
    mains = [i for i in kept if i.role in (Role.MAIN, Role.BREAKFAST, Role.COMBO)]

    # 主食池空了 → 逐条让位(从最不「核心」的约束开始)
    if not mains and active_rules:
        remaining = list(active_rules)
        for rule_name in RELAX_ORDER:
            matched = [r for r in remaining if r[0] == rule_name]
            if not matched:
                continue
            remaining.remove(matched[0])
            relaxations.append(
                f"因可用主食不足,已放宽「{matched[0][1]}」约束 —— 请人工复核被放宽的餐品是否可接受"
            )
            if progress:
                progress(f"放宽约束:{matched[0][1]}")
            kept = filter_items(remaining)
            mains = [i for i in kept if i.role in (Role.MAIN, Role.BREAKFAST, Role.COMBO)]
            if mains:
                break
        active_rules = remaining

    # 营养上限(仅当该餐品有营养数据时才判定,避免误杀)
    if requirements.max_kcal_per_person:
        ceiling = requirements.max_kcal_per_person * max(1, requirements.headcount) * 1.15
        trimmed = []
        for item in kept:
            if item.kcal is not None and item.kcal > ceiling:
                excluded.setdefault(item.name, []).append("单件热量超出整单上限")
                continue
            trimmed.append(item)
        kept = trimmed

    pool = CandidatePool(excluded=excluded, relaxations=relaxations)
    # 同名校验:菜单里偶有同名不同编码(如两个「XX 开心乐园餐」),
    # 合并计价,避免同一餐品被当成两种选择重复推荐
    seen_names: Dict[str, MenuItem] = {}
    for item in kept:
        previous = seen_names.get(item.name)
        if previous is not None:
            previous.aliases.append(previous.code)
            if item.price and (not previous.price or item.price < previous.price):
                previous.price = item.price
                previous.code = item.code
            if item.code not in previous.aliases:
                previous.aliases.append(item.code)
            continue
        seen_names[item.name] = item
        pool.by_role.setdefault(item.role, []).append(item)
    for items in pool.by_role.values():
        items.sort(key=lambda i: (i.price, i.name))

    if requirements.required_codes:
        for code in requirements.required_codes:
            item = catalog.get(code)
            if item is None or item.sold_out:
                pool.relaxations.append(f"指定餐品 {code} 在本店不可售,已忽略")
                continue
            pool.by_role.setdefault(item.role, [])
            if item not in pool.by_role[item.role]:
                pool.by_role[item.role].append(item)

    return pool


# ----------------------------------------------------------------- 打分函数
def preference_score(item: MenuItem, requirements: CateringRequirements) -> float:
    """软偏好得分,越高越契合(0~10)。

    注意:素食/忌口场景下不能继续按「蛋白质越高越好」打分,否则会把
    汉堡推给素食者。这里做了两处保护 —— 素食需求下改用低卡倾向,
    且对无营养数据的餐品不臆造分数。
    """
    score = 0.0
    prefs = list(requirements.preferences)
    if requirements.vegetarian:
        prefs = [p for p in prefs if p != "high_protein"]
        if "low_calorie" not in prefs:
            prefs.append("low_calorie")
    if "low_calorie" in prefs and item.kcal is not None:
        score += max(0.0, 3.0 - item.kcal / 250.0)
    if "high_protein" in prefs and item.protein is not None:
        score += min(3.0, item.protein / 8.0)
    if "low_sodium" in prefs and item.sodium is not None:
        score += max(0.0, 2.0 - item.sodium / 500.0)
    if "filling" in prefs and item.role in (Role.MAIN, Role.COMBO, Role.SIDE):
        score += 1.5
    if "shareable" in prefs and (item.is_combo or "拼" in item.name or "翅" in item.name):
        score += 2.0
    if "coffee" in prefs and item.role is Role.DRINK and any(
            w in item.name for w in ("咖啡", "美式", "奶铁", "拿铁", "卡布")):
        score += 2.0
    if "dessert" in prefs and item.role is Role.DESSERT:
        score += 2.0
    if "refreshing" in prefs and item.role is Role.DRINK and any(
            w in item.name for w in ("柠檬", "红茶", "绿茶", "无糖", "美式")):
        score += 1.5
    if item.role is Role.MAIN:
        score += 0.5
    return score


def score_plan(counts: Dict[str, int], pool: CandidatePool,
               requirements: CateringRequirements, quota: Dict[str, int],
               price_of: Dict[str, int], budget: int = 0) -> Tuple[float, Dict[str, int]]:
    """给一个「code -> 件数」的方案打分(分越低越好)。

    目标函数由三部分构成::

        score = 成本 + 配额缺口罚分 - 软偏好加分 - 预算利用率加分

    * 缺口罚分:主食权重最高(6000 分/件 ≈ 60 元),确保「先吃饱」;
    * 软偏好加分:1 分偏好抵 1.2 元,让「好搭配」压过「纯凑便宜」;
    * **预算利用率加分**:团餐的预算通常必须花掉(不花也不退),
      所以对「预算内花得更充分」的方案给予 0.35 倍的正向加分。
      这部分是实测调出来的:没有它时求解器会为了省钱给人均 220 kcal 的方案。

    返回 ``(score, deficits)``。
    """
    cost = sum(price_of.get(code, 0) * qty for code, qty in counts.items())
    deficits: Dict[str, int] = {}
    penalty = 0.0

    role_of = {item.code: item.role for item in pool.all_items()}
    role_counts: Dict[str, int] = {}
    for code, qty in counts.items():
        role = role_of.get(code)
        if role is None:
            continue
        role_counts[role.value] = role_counts.get(role.value, 0) + qty

    for role_key, needed in quota.items():
        have = role_counts.get(role_key, 0)
        if have < needed:
            deficits[role_key] = needed - have
            penalty += DEFICIT_PENALTY.get(Role(role_key), 800) * (needed - have)

    # 软偏好:整单契合度越高越好,换算成罚分
    items_by_code = {item.code: item for item in pool.all_items()}
    soft = 0.0
    for code, qty in counts.items():
        item = items_by_code.get(code)
        if item is None:
            continue
        soft += preference_score(item, requirements) * qty
    penalty -= soft * 120.0  # 1 分软偏好抵 1.2 元,足以让「好搭配」压过「凑便宜」

    # 单一化惩罚:同一件餐品在同类里占比过高会降低成员满意度
    role_totals: Dict[str, int] = {}
    role_peaks: Dict[str, int] = {}
    for code, qty in counts.items():
        item = items_by_code.get(code)
        if item is None:
            continue
        key = item.role.value
        role_totals[key] = role_totals.get(key, 0) + qty
        role_peaks[key] = max(role_peaks.get(key, 0), qty)
    for role_key, cap_ratio in VARIETY_CAP.items():
        total = role_totals.get(role_key, 0)
        if total >= 4:
            peak_ratio = role_peaks.get(role_key, 0) / total
            if peak_ratio > cap_ratio:
                penalty += 4000.0 * (peak_ratio - cap_ratio)

    # 预算利用率:预算内花得越充分越优(团餐预算通常不退,凑太省反而不实用)
    if budget and budget < 10 ** 8 and cost <= budget:
        penalty -= cost * 0.35

    return cost + penalty, deficits


# ------------------------------------------------------------------ 主求解器
class CateringPlanner:
    """团餐方案求解器。"""

    def __init__(self, catalog: MenuCatalog, requirements: CateringRequirements,
                 *, coupons: Optional[Sequence[Coupon]] = None,
                 promotions: Optional[Sequence[Promotion]] = None,
                 progress: ProgressFn = None) -> None:
        self.catalog = catalog
        self.requirements = requirements
        self.coupons = list(coupons or [])
        self.promotions = list(promotions or [])
        self.progress = progress
        self.pool = build_candidate_pool(catalog, requirements, progress=progress)
        self.quota = role_quota(requirements)
        self.price_of = {item.code: item.price for item in self.pool.all_items()}

    # ------------------------------------------------------------ 装配与搜索
    def _random_seed_plan(self, rng: random.Random) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for role_key, needed in self.quota.items():
            pool = self.pool.pick(Role(role_key))
            if not pool:
                continue
            # 偏好加权抽样:契合度高的更容易被选中
            weights = [1.0 + preference_score(i, self.requirements) for i in pool]
            for _ in range(needed):
                item = rng.choices(pool, weights=weights, k=1)[0]
                counts[item.code] = counts.get(item.code, 0) + 1
        for code in self.requirements.required_codes:
            if code in self.price_of:
                counts[code] = max(1, counts.get(code, 0))
        return counts

    def _local_search(self, counts: Dict[str, int], rng: random.Random,
                      budget: int) -> Dict[str, int]:
        """局部搜索:修预算、换便宜、加件。"""
        counts = dict(counts)

        def cost(plan: Dict[str, int]) -> int:
            return sum(self.price_of.get(code, 0) * qty for code, qty in plan.items())

        # 阶段一:压到预算内 —— 反复替换为更便宜的同类,仍超则减件
        guard = 0
        while cost(counts) > budget and guard < 400:
            guard += 1
            items_by_code = {i.code: i for i in self.pool.all_items()}
            candidates = [(code, qty) for code, qty in counts.items()
                          if items_by_code.get(code) and qty > 0]
            if not candidates:
                break
            # 优先替换「单价最高且同角色有更便宜替代」的行
            candidates.sort(key=lambda kv: -items_by_code[kv[0]].price)
            changed = False
            for code, qty in candidates:
                item = items_by_code[code]
                cheaper = [i for i in self.pool.pick(item.role) if i.price < item.price]
                if cheaper:
                    replacement = min(cheaper, key=lambda i: i.price)
                    counts[code] -= 1
                    counts[replacement.code] = counts.get(replacement.code, 0) + 1
                    changed = True
                    break
            if not changed:
                # 没有更便宜的同类,只能砍掉最贵的一件
                code, _ = candidates[0]
                counts[code] -= 1
            counts = {c: q for c, q in counts.items() if q > 0}

        # 阶段二:在预算内做「换更便宜」的随机改进
        for _ in range(120):
            items_by_code = {i.code: i for i in self.pool.all_items()}
            codes = [c for c in counts if items_by_code.get(c)]
            if not codes:
                break
            code = rng.choice(codes)
            item = items_by_code[code]
            pool = self.pool.pick(item.role)
            if len(pool) < 2:
                continue
            alternative = rng.choice(pool)
            if alternative.code == code:
                continue
            old_score, _ = score_plan(counts, self.pool, self.requirements, self.quota, self.price_of, budget)
            trial = dict(counts)
            trial[code] -= 1
            trial[alternative.code] = trial.get(alternative.code, 0) + 1
            trial = {c: q for c, q in trial.items() if q > 0}
            if cost(trial) > budget:
                continue
            new_score, _ = score_plan(trial, self.pool, self.requirements, self.quota, self.price_of, budget)
            if new_score < old_score:
                counts = trial

        # 阶段三:预算有余量时补齐缺口 / 加最有价值的件
        for _ in range(200):
            remaining = budget - cost(counts)
            if remaining <= 0:
                break
            _, deficits = score_plan(counts, self.pool, self.requirements, self.quota, self.price_of, budget)
            items_by_code = {i.code: i for i in self.pool.all_items()}
            options: List[MenuItem] = []
            if deficits:
                for role_key, missing in deficits.items():
                    options.extend(self.pool.pick(Role(role_key)))
            else:
                # 已无缺口:只加「真正的体验项」,且每类不超过配额的 1.5 倍,
                # 避免把剩余预算全砸成同一种便宜小食(实测踩坑点)
                role_counts: Dict[str, int] = {}
                for code, qty in counts.items():
                    item = items_by_code.get(code)
                    if item is not None:
                        role_counts[item.role.value] = role_counts.get(item.role.value, 0) + qty
                for role in (Role.SIDE, Role.DESSERT, Role.DRINK):
                    quota_for_role = self.quota.get(role.value, 0)
                    cap = max(1, int(round(quota_for_role * 1.5)))
                    if role_counts.get(role.value, 0) < cap:
                        options.extend(self.pool.pick(role))
            options = [i for i in options if 0 < i.price <= remaining]
            # 多样性硬上限:同一件餐品不得超过其角色配额的 VARIETY_CAP 比例,
            # 否则求解器会把一次团餐全买成同一种最便宜的汉堡
            capped: List[MenuItem] = []
            for item in options:
                ratio = VARIETY_CAP.get(item.role)
                if ratio:
                    limit = max(1, int(round(self.quota.get(item.role.value, 0) * ratio)))
                    if counts.get(item.code, 0) >= limit:
                        continue
                capped.append(item)
            options = capped
            if not options:
                break
            best, best_score = None, None
            for item in options[:60]:
                trial = dict(counts)
                trial[item.code] = trial.get(item.code, 0) + 1
                value, _ = score_plan(trial, self.pool, self.requirements, self.quota, self.price_of, budget)
                if best_score is None or value < best_score:
                    best, best_score = item, value
            if best is None:
                break
            counts[best.code] = counts.get(best.code, 0) + 1

        return counts

    def solve(self) -> CateringPlan:
        """执行多起点局部搜索,返回最优方案。"""
        budget = self.requirements.budget_cents or 10 ** 9
        rng = random.Random(self.requirements.random_seed)
        best_counts: Dict[str, int] = {}
        best_score = float("inf")

        for attempt in range(max(1, self.requirements.effort)):
            seed = self._random_seed_plan(rng)
            candidate = self._local_search(seed, rng, budget)
            value, _ = score_plan(candidate, self.pool, self.requirements, self.quota, self.price_of, budget)
            cost = sum(self.price_of.get(c, 0) * q for c, q in candidate.items())
            if cost > budget:
                value += 10 ** 6  # 超预算的方案直接重罚
            if value < best_score:
                best_score, best_counts = value, candidate
            if self.progress and attempt % 12 == 0:
                self.progress(f"方案搜索 {attempt + 1}/{self.requirements.effort}")

        plan = self._materialize(best_counts)
        plan.score = best_score
        return plan

    # -------------------------------------------------------------- 结果组装
    def _materialize(self, counts: Dict[str, int]) -> CateringPlan:
        lines: List[PlanLine] = []
        for code, qty in sorted(counts.items(), key=lambda kv: -self.price_of.get(kv[0], 0)):
            item = self.catalog.get(code)
            if item is None or qty <= 0:
                continue
            lines.append(PlanLine(item=item, quantity=qty))

        plan = CateringPlan(
            store_code=self.catalog.store_code,
            store_name=self.catalog.store_name,
            headcount=self.requirements.headcount,
            lines=lines,
            budget_cents=self.requirements.budget_cents,
            relaxations=list(self.pool.relaxations),
            warnings=[],
        )
        plan.recalc()
        self._apply_coupons(plan)
        self._check_targets(plan)
        return plan

    # ------------------------------------------------------------- 券与促销
    def _apply_coupons(self, plan: CateringPlan) -> None:
        """在候选方案上尝试叠加门店券,只接受**确实降低应付**的券。"""
        if not self.coupons:
            return
        active = [c for c in self.coupons if c.is_active()]
        best_coupon: Optional[Coupon] = None
        best_saving = 0

        for coupon in active:
            if coupon.kind not in ("threshold", "discount"):
                continue
            saving = 0
            if coupon.kind == "threshold" and coupon.discount_yuan:
                if plan.subtotal_cents >= int(coupon.threshold_yuan * 100):
                    saving = int(coupon.discount_yuan * 100)
            elif coupon.kind == "discount" and coupon.discount_rate:
                saving = int(plan.subtotal_cents * (1 - coupon.discount_rate))
            if saving > best_saving:
                best_saving, best_coupon = saving, coupon

        if best_coupon and best_saving > 0:
            plan.coupons_applied.append(best_coupon)
            plan.discount_cents = best_saving
            plan.payable_cents = max(0, plan.subtotal_cents - best_saving)

        # 记录那些「够不着」的门槛券,便于人工判断是否值得加单
        for coupon in active:
            if coupon.kind == "threshold" and coupon.discount_yuan:
                gap = int(coupon.threshold_yuan * 100) - plan.subtotal_cents
                if 0 < gap <= 4000:
                    plan.warn(
                        "coupon_gap",
                        f"再补 ¥{gap / 100:.2f} 可用「{coupon.title}」省 ¥{coupon.discount_yuan:g}",
                    )

    def _check_targets(self, plan: CateringPlan) -> None:
        totals = plan.nutrition_totals()
        counted = int(totals.get("_counted_quantity", 0))
        if counted < plan.total_quantity:
            missing = plan.total_quantity - counted
            plan.warn(
                "nutrition_gap",
                f"有 {missing} 件餐品缺少官方营养数据,营养合计为下限值而非精确值",
            )
        req = self.requirements
        if req.max_kcal_per_person and plan.per_person("kcal") > req.max_kcal_per_person:
            plan.warn(
                "kcal_target",
                f"人均热量 {plan.per_person('kcal'):g} kcal 超出目标 "
                f"{req.max_kcal_per_person:g} kcal,可减少小食/甜品或改用无糖饮品",
            )
        if req.min_protein_per_person and plan.per_person("protein") < req.min_protein_per_person:
            plan.warn(
                "protein_target",
                f"人均蛋白质 {plan.per_person('protein'):g} g 低于目标 "
                f"{req.min_protein_per_person:g} g,建议增加汉堡/鸡胸类主食",
            )
        if req.max_sodium_per_person and plan.per_person("sodium") > req.max_sodium_per_person:
            plan.warn(
                "sodium_target",
                f"人均钠 {plan.per_person('sodium'):g} mg 高于目标 "
                f"{req.max_sodium_per_person:g} mg,注意腌制类与酱料",
            )


def verify_plan_with_api(client, store_context, plan: CateringPlan,
                         *, progress: ProgressFn = None) -> CateringPlan:
    """用 ``calculate-price`` 回算真实价格,覆盖本地加总。

    这是本项目「不做假账」的关键一步:本地按单价×数量相加只是估算,
    真实促销、套餐价、券抵扣都由服务端计算。若接口返回的价格与本地差异
    超过 1 分,以接口为准并写入 ``warnings`` 说明差异来源。
    """
    if not plan.lines:
        return plan
    items = [{"productCode": line.item.code, "quantity": line.quantity}
             for line in plan.lines]
    if progress:
        progress("向 calculate-price 回算真实总价")
    try:
        data = client.calculate_price(
            store_context.store_code, items,
            order_type=store_context.order_type,
            be_type=store_context.be_type,
            be_code=store_context.be_code,
            reservation_date=store_context.reservation_date,
        )
    except Exception as exc:  # noqa: BLE001 - 回算失败不影响方案可用性
        plan.warn("verify_failed", f"服务端价格回算失败,当前金额为本地估算:{exc}")
        return plan

    api_subtotal = int(data.get("originalPrice") or data.get("productOriginalPrice") or 0)
    api_payable = int(data.get("price") or data.get("productPrice") or 0)
    api_discount = int(data.get("discount") or 0)
    local_subtotal = plan.subtotal_cents
    if api_subtotal:
        plan.subtotal_cents = api_subtotal
        # 保留本店快照价,便于报告里如实说明「预估 vs 门店实价」的差异来源
        plan.local_estimate_cents = local_subtotal
        plan.clear_warnings("price_diff")
        if api_subtotal != local_subtotal:
            plan.warn(
                "price_diff",
                f"门店实时原价 ¥{api_subtotal / 100:.2f} 与本地估算 "
                f"¥{local_subtotal / 100:.2f} 相差 "
                f"¥{abs(api_subtotal - local_subtotal) / 100:.2f}"
                "(促销、规格或取餐方式导致),已以门店价为准",
            )
    if api_payable:
        plan.payable_cents = api_payable
    if api_discount:
        plan.discount_cents = max(plan.discount_cents, api_discount)
    plan.verified_by_api = True
    plan.api_trace.append("calculate-price 已回算")
    plan.recalc()
    plan.clear_warnings("over_budget")
    if plan.budget_cents and plan.payable_cents > plan.budget_cents:
        plan.warn(
            "over_budget",
            f"门店实时价高于本地估算,当前超预算 ¥"
            f"{(plan.payable_cents - plan.budget_cents) / 100:,.2f};"
            "可下调人数/提高预算,或减少小食与饮料",
        )
    return plan


def fit_plan_to_budget(client, store_context, plan: CateringPlan, *,
                       max_rounds: int = 4, progress: ProgressFn = None) -> CateringPlan:
    """门店实价高于本地估算时,自动削减小食/饮料直到回到预算内。

    为什么需要这一步:菜单价格快照来自上一次查询,门店促销、规格或取餐方式
    变化都会让实价与快照不同。只「如实报告超预算」而不做收敛,对使用者没价值,
    所以这里按「先砍溢价小食 → 再砍饮料 → 最后才动主食」的顺序自动调整,
    每一轮都用 ``calculate-price`` 重新确认,并记录被砍掉的条目。

    主食配额是最后防线:砍到主食低于人数时停止,并在 ``warnings`` 里说明,
    把决定权交回用户。
    """
    if not plan.budget_cents or plan.payable_cents <= plan.budget_cents:
        return plan

    for round_index in range(max(1, max_rounds)):
        over = plan.payable_cents - plan.budget_cents
        if over <= 0:
            break
        removal_order = [Role.DESSERT, Role.SIDE, Role.DRINK]
        main_lines = [l for l in plan.lines if l.item.role is Role.MAIN]
        main_count = sum(l.quantity for l in main_lines)
        can_touch_main = main_count > plan.headcount

        target: Optional[PlanLine] = None
        for role in removal_order:
            candidates = [l for l in plan.lines if l.item.role is role and l.quantity > 0]
            if candidates:
                target = max(candidates, key=lambda l: l.subtotal)
                break
        if target is None and can_touch_main:
            target = max(main_lines, key=lambda l: l.subtotal)
        if target is None:
            plan.warn("no_cut", "已无可削减的非主食项,请在预算或人数上做取舍")
            break

        target.quantity -= 1
        plan.lines = [l for l in plan.lines if l.quantity > 0]
        plan.recalc()
        plan.warn("auto_cut", f"为回到预算内,已减少 1 份「{target.item.name}」")
        if progress:
            progress(f"超预算 ¥{over / 100:.2f},削减 1 份「{target.item.name}」并重新核价")
        if not plan.lines:
            break
        plan = verify_plan_with_api(client, store_context, plan)

    if plan.payable_cents > plan.budget_cents:
        plan.warn(
            "still_over_budget",
            f"仍超出预算 ¥{(plan.payable_cents - plan.budget_cents) / 100:,.2f},"
            "建议提高预算或减少人数",
        )
    return plan


def cheapest_per_role(catalog: MenuCatalog, requirements: CateringRequirements
                      ) -> Dict[str, MenuItem]:
    """给出每个角色的最低价选择,用于报告里的「性价比锚点」。"""
    pool = build_candidate_pool(catalog, requirements)
    out: Dict[str, MenuItem] = {}
    for role in Role:
        items = pool.pick(role)
        if items:
            out[role.label] = min(items, key=lambda i: (i.price, -i.protein_per_yuan()))
    return out
