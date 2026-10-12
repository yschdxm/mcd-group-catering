"""团餐需求模型与中文自然语言场合解析。

团餐真正难的地方不是「算钱」,而是把一句口语需求翻译成可计算的约束:

    周五中午 12 点前送到,14 个人,预算 480,两人在减脂要低卡,
    一个不吃辣,一个不吃牛肉,再来点能分着吃的

本模块负责把这句话变成 :class:`CateringRequirements`:
人数、预算(分为单位)、送达时间、硬性忌口、软性偏好、营养目标。
所有解析结果都保留 ``notes`` 说明,以便在报告里回显「我是这样理解你的需求的」,
避免算得对但理解错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from ..mcp.context import FulfillmentMode

_CN_NUM = {
    "零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12, "十三": 13,
    "十四": 14, "十五": 15, "十六": 16, "十七": 17, "十八": 18, "十九": 19,
    "二十": 20, "二十一": 21, "二十二": 22, "二十三": 23, "二十四": 24,
    "二十五": 25, "二十六": 26, "二十七": 27, "二十八": 28, "二十九": 29,
    "三十": 30, "四十": 40, "五十": 50, "六十": 60, "八十": 80, "一百": 100,
}

_HEADCOUNT_RE = re.compile(
    r"(?P<num>\d+|[零一两二三四五六七八九十百]{1,3})\s*(?:个)?\s*(?:人|位|份|员|同事)"
)
_BUDGET_TOTAL_RE = re.compile(
    r"(?:预算|总预算|总额|总共|一共|花费|控制在|不超过)\s*(?:是|为|在|:)?\s*"
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>元|块|块钱|rmb|RMB)?"
)
_BUDGET_PER_RE = re.compile(
    r"(?:人均|每人|每位|平均每人)\s*(?:预算|控制在|不超过|大概|约)?\s*"
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>元|块|块钱)?"
)
_TIME_RE = re.compile(r"(?P<h>\d{1,2})\s*[:：点]\s*(?P<m>\d{1,2})?\s*分?")
_CALORIE_PER_RE = re.compile(r"(?:人均|每人|每位)\s*(?:不超过|低于|控制在|约)?\s*(?P<num>\d{3,4})\s*(?:大卡|千卡|kcal|卡)")
_CALORIE_TOTAL_RE = re.compile(r"(?:总共|合计|一共|总量?)\s*(?P<num>\d{4,5})\s*(?:大卡|千卡|kcal|卡)")
_PROTEIN_PER_RE = re.compile(r"(?:人均|每人|每位)\s*(?:至少|不低于|要)?\s*(?P<num>\d{1,3})\s*克?\s*(?:蛋白质|蛋白)")
_SODIUM_PER_RE = re.compile(r"(?:人均|每人|每位)\s*(?:不超过|低于|控制在)\s*(?P<num>\d{3,5})\s*(?:毫克|mg)?\s*钠")
_KID_COUNT_RE = re.compile(r"(?P<num>\d+|[一两二三四五六七八九十]{1,2})\s*(?:个|位)?\s*(?:小朋友|小孩|儿童|宝宝)")

#: 忌口词 -> 约束字段。顺序无关,全部命中即全部生效
_AVOID_RULES: List[Tuple[Tuple[str, ...], str]] = [
    (("不吃辣", "不要辣", "不能吃辣", "忌辣", "免辣", "微辣也不行"), "avoid_spicy"),
    (("不吃牛", "牛肉过敏", "不吃牛肉", "忌牛"), "avoid_beef"),
    (("不吃猪", "猪肉过敏", "不吃猪肉", "忌猪", "清真", "回民", "穆斯林"), "avoid_pork"),
    (("不吃鱼", "海鲜过敏", "不吃海鲜", "不吃鱼虾", "忌鱼"), "avoid_fish"),
    (("不吃鸡", "鸡肉过敏", "不吃鸡肉", "忌鸡"), "avoid_chicken"),
    (("不吃蛋", "鸡蛋过敏", "不吃鸡蛋", "忌蛋"), "avoid_egg"),
    (("乳糖不耐", "不吃奶", "不喝奶", "不吃乳", "乳制品过敏", "牛奶过敏"), "avoid_dairy"),
    (("素食", "吃素", "全素", "纯素"), "vegetarian"),
]

#: 软性偏好词 -> 口味标签
_PREFERENCE_RULES: List[Tuple[Tuple[str, ...], str]] = [
    (("低卡", "低热量", "减脂", "减肥", "控卡", "轻食", "健身"), "low_calorie"),
    (("高蛋白", "增肌", "蛋白"), "high_protein"),
    (("少盐", "低钠", "控盐", "少油"), "low_sodium"),
    (("饱腹", "管饱", "分量足", "能吃饱", "干活"), "filling"),
    (("小食", "拼盘", "分享", "分着吃", "多人餐"), "shareable"),
    (("咖啡", "提神", "续命"), "coffee"),
    (("甜", "甜品", "冰", "冰淇淋"), "dessert"),
    (("清爽", "解腻", "轻口"), "refreshing"),
]

_MEAL_TIME_WORDS = {
    "breakfast": ("早餐", "早上", "早晨", "一早"),
    "lunch": ("中午", "午餐", "午饭", "午间"),
    "afternoon": ("下午", "下午茶", "茶歇"),
    "dinner": ("晚上", "晚餐", "晚饭", "加班"),
    "midnight": ("夜宵", "宵夜", "夜里", "凌晨"),
}


def _infer_meal_time(hour: int) -> str:
    """只给了送达时刻时,按小时推断用餐时段。

    时段会影响配比:早餐少配小食、下午茶要配甜点与饮品、夜宵需要咖啡提神。
    """
    if 5 <= hour <= 9:
        return "breakfast"
    if 10 <= hour <= 11:
        return "lunch"
    if 12 <= hour <= 14:
        return "lunch"
    if 15 <= hour <= 17:
        return "afternoon"
    if 18 <= hour <= 21:
        return "dinner"
    return "midnight"


@dataclass
class CateringRequirements:
    """一次团餐统筹的完整输入。"""

    headcount: int = 10
    budget_total: Optional[float] = None      # 单位:元
    budget_per_person: Optional[float] = None  # 单位:元
    mode: FulfillmentMode = FulfillmentMode.GROUP
    city: str = ""
    keyword: str = ""
    address_id: str = ""
    store_code: str = ""
    reserve_at: Optional[datetime] = None
    meal_time: str = ""
    kids: int = 0

    # 硬约束
    avoid_spicy: bool = False
    avoid_beef: bool = False
    avoid_pork: bool = False
    avoid_fish: bool = False
    avoid_chicken: bool = False
    avoid_egg: bool = False
    avoid_dairy: bool = False
    vegetarian: bool = False
    required_codes: List[str] = field(default_factory=list)
    excluded_codes: List[str] = field(default_factory=list)

    # 软偏好
    preferences: List[str] = field(default_factory=list)

    # 营养目标(按人)
    max_kcal_per_person: Optional[float] = None
    min_protein_per_person: Optional[float] = None
    max_sodium_per_person: Optional[float] = None

    # 求解参数
    preferred_roles: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    random_seed: int = 20261025
    effort: int = 48          # 局部搜索重启次数,越大越慢越优

    # ---------------------------------------------------------------- 派生量
    @property
    def budget_yuan(self) -> float:
        """总预算(元)。优先取总预算,其次由人均推算。"""
        if self.budget_total is not None:
            return float(self.budget_total)
        if self.budget_per_person is not None:
            return float(self.budget_per_person) * max(1, self.headcount)
        return 0.0

    @property
    def budget_cents(self) -> int:
        return int(round(self.budget_yuan * 100))

    @property
    def per_person_yuan(self) -> float:
        if self.budget_per_person is not None:
            return float(self.budget_per_person)
        if self.budget_total is not None and self.headcount:
            return round(float(self.budget_total) / self.headcount, 2)
        return 0.0

    @property
    def has_hard_dietary(self) -> bool:
        return any([
            self.avoid_spicy, self.avoid_beef, self.avoid_pork, self.avoid_fish,
            self.avoid_chicken, self.avoid_egg, self.avoid_dairy, self.vegetarian,
        ])

    def hard_rule_summary(self) -> List[str]:
        labels = {
            "avoid_spicy": "不吃辣", "avoid_beef": "不吃牛肉", "avoid_pork": "不吃猪肉",
            "avoid_fish": "不吃鱼类海鲜", "avoid_chicken": "不吃鸡肉",
            "avoid_egg": "不吃蛋", "avoid_dairy": "乳制品不耐受", "vegetarian": "素食",
        }
        return [text for key, text in labels.items() if getattr(self, key)]

    def soft_rule_summary(self) -> List[str]:
        labels = {
            "low_calorie": "低卡优先", "high_protein": "高蛋白优先",
            "low_sodium": "少盐优先", "filling": "饱腹优先",
            "shareable": "需要可分食的小食/拼盘", "coffee": "需要咖啡",
            "dessert": "需要甜品", "refreshing": "清爽解腻",
        }
        return [labels.get(p, p) for p in self.preferences]

    def to_dict(self) -> Dict[str, object]:
        return {
            "headcount": self.headcount,
            "kids": self.kids,
            "budgetTotalYuan": self.budget_total,
            "budgetPerPersonYuan": self.budget_per_person,
            "budgetYuan": self.budget_yuan,
            "mode": self.mode.value,
            "mealTime": self.meal_time,
            "reserveAt": self.reserve_at.strftime("%Y-%m-%d %H:%M") if self.reserve_at else None,
            "hardRules": self.hard_rule_summary(),
            "softRules": self.soft_rule_summary(),
            "targets": {
                "maxKcalPerPerson": self.max_kcal_per_person,
                "minProteinPerPerson": self.min_protein_per_person,
                "maxSodiumPerPerson": self.max_sodium_per_person,
            },
            "notes": list(self.notes),
        }


def _cn_number(text: str) -> Optional[int]:
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in _CN_NUM:
        return _CN_NUM[text]
    # 处理「二十三」这类组合
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_NUM.get(head, 1) if head else 1
        ones = _CN_NUM.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return _CN_NUM.get(text)


def parse_requirements(text: str, *, base: Optional[CateringRequirements] = None,
                       now: Optional[datetime] = None) -> CateringRequirements:
    """把一句中文团餐需求解析成 :class:`CateringRequirements`。

    设计取舍:宁可「少解析几个字段并留下 note」,也不要静默猜错。
    每个成功识别的片段都会追加一条 ``notes``,报告中会原样回显。
    """
    requirements = base or CateringRequirements()
    raw = text or ""
    moment = now or datetime.now()

    head = _HEADCOUNT_RE.search(raw)
    if head:
        value = _cn_number(head.group("num"))
        if value:
            requirements.headcount = value
            requirements.notes.append(f"人数 = {value}")

    per = _BUDGET_PER_RE.search(raw)
    if per:
        requirements.budget_per_person = float(per.group("num"))
        requirements.notes.append(f"人均预算 = {requirements.budget_per_person:g} 元")

    total = _BUDGET_TOTAL_RE.search(raw)
    if total:
        # 避免把「人均预算 35」也当成总预算
        span_is_per = bool(per and per.start() <= total.start() <= per.end())
        if not span_is_per:
            requirements.budget_total = float(total.group("num"))
            requirements.notes.append(f"总预算 = {requirements.budget_total:g} 元")

    kids = _KID_COUNT_RE.search(raw)
    if kids:
        value = _cn_number(kids.group("num")) or 0
        requirements.kids = value
        requirements.notes.append(f"其中儿童 {value} 人")

    for key, words in _MEAL_TIME_WORDS.items():
        if any(word in raw for word in words):
            requirements.meal_time = key
            requirements.notes.append(f"用餐时段 = {key}")
            break

    clock = _TIME_RE.search(raw)
    if clock:
        hour = int(clock.group("h"))
        minute = int(clock.group("m") or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            day = moment.date()
            if "明天" in raw:
                day = (moment + timedelta(days=1)).date()
            elif "后天" in raw:
                day = (moment + timedelta(days=2)).date()
            candidate = datetime.combine(day, datetime.min.time()).replace(
                hour=hour, minute=minute)
            if candidate <= moment and "明天" not in raw and "后天" not in raw:
                # 时间已过,视为次日
                candidate += timedelta(days=1)
            requirements.reserve_at = candidate
            requirements.notes.append(f"送达/取餐时间 = {candidate:%Y-%m-%d %H:%M}")
            if not requirements.meal_time:
                # 只给了时刻时,按小时推断用餐时段,用于调整饮料/甜品配比
                requirements.meal_time = _infer_meal_time(hour)
                if requirements.meal_time:
                    requirements.notes.append(f"用餐时段(按时间推断) = {requirements.meal_time}")

    calorie = _CALORIE_PER_RE.search(raw)
    if calorie:
        requirements.max_kcal_per_person = float(calorie.group("num"))
        requirements.notes.append(f"人均热量上限 = {requirements.max_kcal_per_person:g} kcal")
    elif (total_cal := _CALORIE_TOTAL_RE.search(raw)):
        value = float(total_cal.group("num"))
        requirements.max_kcal_per_person = round(value / max(1, requirements.headcount), 1)
        requirements.notes.append(
            f"总热量上限 {value:g} kcal → 人均 {requirements.max_kcal_per_person:g} kcal")

    protein = _PROTEIN_PER_RE.search(raw)
    if protein:
        requirements.min_protein_per_person = float(protein.group("num"))
        requirements.notes.append(f"人均蛋白质下限 = {requirements.min_protein_per_person:g} g")

    sodium = _SODIUM_PER_RE.search(raw)
    if sodium:
        requirements.max_sodium_per_person = float(sodium.group("num"))
        requirements.notes.append(f"人均钠上限 = {requirements.max_sodium_per_person:g} mg")

    for words, attribute in _AVOID_RULES:
        if any(word in raw for word in words):
            if not getattr(requirements, attribute):
                setattr(requirements, attribute, True)

    for words, preference in _PREFERENCE_RULES:
        if any(word in raw for word in words) and preference not in requirements.preferences:
            requirements.preferences.append(preference)

    # 明确提到某个餐品名时,视为「必须包含」
    for word, code_map in (("巨无霸", "1100"), ("麦辣鸡腿汉堡", "1440"),
                           ("板烧鸡腿堡", "1406"), ("中薯条", None)):
        if word in raw and code_map and code_map not in requirements.required_codes:
            requirements.required_codes.append(code_map)

    if any(word in raw for word in ("外送", "送到", "配送", "麦乐送")):
        requirements.mode = FulfillmentMode.DELIVERY
    elif any(word in raw for word in ("团餐", "企业", "公司", "部门")):
        requirements.mode = FulfillmentMode.GROUP
    elif any(word in raw for word in ("得来速", "车道")):
        requirements.mode = FulfillmentMode.DRIVE_THRU
    elif any(word in raw for word in ("自取", "到店", "堂食", "打包")):
        requirements.mode = FulfillmentMode.PICKUP

    for city in ("北京", "上海", "广州", "深圳", "无锡", "青岛", "南京", "杭州",
                 "成都", "武汉", "西安", "重庆", "天津", "苏州", "长沙"):
        if city in raw:
            requirements.city = city
            requirements.notes.append(f"城市 = {city}")
            break

    if requirements.has_hard_dietary:
        requirements.notes.append("硬性忌口:" + "、".join(requirements.hard_rule_summary()))
    if requirements.preferences:
        requirements.notes.append("软性偏好:" + "、".join(requirements.soft_rule_summary()))

    return requirements


def role_quota(requirements: CateringRequirements) -> Dict[str, int]:
    """根据人数与场合推算各角色需要的件数。

    这是团餐的「配比常识」,公式公开可核对:

    * **主食**:每人 1 份(儿童按 0.7 份计)。这是唯一不允许打折的配额 ——
      团餐最容易翻车的地方就是「小食买一堆、主食不够吃」。官方也在
      ``query-meals`` 的说明里给出过团餐搭配建议(按人均预算选择
      汉堡+小食+饮料的组合),本函数与其思路一致。
    * **饮料**:默认不强制(很多团队自带水),但若需求里出现「饮料/可乐/咖啡」
      等字样或时段为下午茶,则按人数配置;基准为 0.7 份/人。
    * **小食**:每 3 人 1 份;偏好「饱腹/拼盘/可分食」时提升到每 2 人 1 份。
    * **甜品**:默认 0,仅在明确提到或下午茶/有儿童时配置。

    刻意把「主食」设成硬缺口,把其余角色设成软配额:违反软配额只扣分,
    违反主食配额会被重罚,从而避免出现「14 份冰淇淋配 2 个汉堡」的方案。
    """
    people = max(1, requirements.headcount)
    adults = max(0, people - requirements.kids)
    main = adults + max(0, requirements.kids) * 0.7
    quota = {
        "main": max(1, int(round(main))),
        "drink": 0,
        "side": max(1, int(round(people / 3))),
        "dessert": 0,
    }

    wants_drink = (
        requirements.meal_time in ("afternoon",)
        or any(word in " ".join(requirements.notes) for word in ("饮料", "可乐", "咖啡"))
        or "coffee" in requirements.preferences
        or "refreshing" in requirements.preferences
    )
    if wants_drink or people >= 6:
        quota["drink"] = max(1, int(round(people * 0.7)))

    if "shareable" in requirements.preferences or "filling" in requirements.preferences:
        quota["side"] = max(quota["side"], int(round(people / 2)))
    if requirements.meal_time == "afternoon":
        quota["drink"] = max(1, int(round(people)))
        quota["dessert"] = max(1, int(round(people / 3)))
    if requirements.meal_time == "breakfast":
        quota["side"] = max(1, int(round(people / 4)))
    if "dessert" in requirements.preferences:
        quota["dessert"] = max(1, int(round(people / 4)))
    if requirements.kids >= 2:
        quota["dessert"] = max(quota["dessert"], max(1, int(round(requirements.kids / 2))))
    return quota
