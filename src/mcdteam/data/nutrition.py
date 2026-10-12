"""营养数据解析与匹配。

``list-nutrition-foods`` 是麦当劳 MCP 里最「不像 API」的一个工具:
它返回的不是 JSON,而是一段**自定义文本表格**::

    [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
      猪柳麦满分,null,1288,308,16,16,24,781,213
      中薯条,null,1210,289,4,12,38,165,18

注意三个真实存在的坑:

1. 首行是 ``[行数]{列名}:`` 的表头,**必须解析而不能写死行数**。
2. ``nutritionDescription`` 列可能是 ``null``,也可能是带逗号的说明文本
   (例如 ``能量约鲈鱼1条``),所以不能简单地按逗号切完就取固定下标。
3. 个别行会重复出现(如 ``小杯玉米杯``、``纯牛奶（盒装）``),需要去重。

单位:``energyKj`` 千焦、``energyKcal`` 千卡、``protein``/``fat``/``carbohydrate``
克、``sodium``/``calcium`` 毫克。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

_HEADER_RE = re.compile(r"^\s*\[(?P<count>\d+)\]\s*\{(?P<columns>[^}]*)\}\s*:?\s*$")
_NUMBER = r"-?\d+(?:\.\d+)?"

#: 列名顺序在服务端是稳定的,但仍然按表头动态定位,避免将来调序导致静默错位
CANONICAL_COLUMNS = [
    "productName",
    "nutritionDescription",
    "energyKj",
    "energyKcal",
    "protein",
    "fat",
    "carbohydrate",
    "sodium",
    "calcium",
]

#: 名称归一化时统一删除的装饰性字符
_NOISE_RE = re.compile(r"[\s\u3000·・\-—_/\\|()（）\[\]【】“”\"'’‘:：,，.。!！?？+＋*#]")


def normalize_name(raw: str) -> str:
    """名称归一化:全角转半角 + 去装饰字符 + 小写。

    用于把 ``【美汁源】“黄金橙橙”`` 与 ``美汁源黄金橙橙`` 这类写法对齐。
    """
    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    text = text.replace("（", "(").replace("）", ")")
    text = _NOISE_RE.sub("", text)
    return text.lower()


def _to_float(value: str) -> Optional[float]:
    value = (value or "").strip()
    if not value or value.lower() in {"null", "none", "na", "-"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Nutrition:
    """一个餐品的营养成分。"""

    product_name: str
    energy_kj: Optional[float] = None
    energy_kcal: Optional[float] = None
    protein: Optional[float] = None
    fat: Optional[float] = None
    carbohydrate: Optional[float] = None
    sodium: Optional[float] = None
    calcium: Optional[float] = None
    description: Optional[str] = None

    @property
    def key(self) -> str:
        return normalize_name(self.product_name)

    def to_dict(self) -> Dict[str, Optional[float]]:
        return {
            "productName": self.product_name,
            "energyKcal": self.energy_kcal,
            "energyKj": self.energy_kj,
            "protein": self.protein,
            "fat": self.fat,
            "carbohydrate": self.carbohydrate,
            "sodium": self.sodium,
            "calcium": self.calcium,
            "description": self.description,
        }


def parse_nutrition_table(raw: str) -> List[Nutrition]:
    """解析 ``list-nutrition-foods`` 的文本表格。

    :raises ValueError: 输入为空或找不到表头。
    """
    if not raw or not raw.strip():
        raise ValueError("营养数据为空(该工具实测存在偶发空返回,请重试或使用快照)")

    # 该接口返回的文本在部分客户端/落盘环节会带上 BOM,先剥掉再匹配表头
    raw = raw.lstrip("\ufeff")

    columns: Optional[List[str]] = None
    rows: List[Nutrition] = []

    for line in raw.splitlines():
        if not line.strip():
            continue
        header = _HEADER_RE.match(line)
        if header:
            columns = [c.strip() for c in header.group("columns").split(",") if c.strip()]
            if "productName" not in columns:
                raise ValueError(f"营养表头缺少 productName:{columns}")
            continue
        if columns is None:
            # 表头之前的说明文字直接跳过
            continue

        cells = _split_cells(line.strip(), len(columns))
        if cells is None:
            continue
        record = dict(zip(columns, cells))
        rows.append(Nutrition(
            product_name=record.get("productName", "").strip(),
            description=_clean_optional(record.get("nutritionDescription")),
            energy_kj=_to_float(record.get("energyKj", "")),
            energy_kcal=_to_float(record.get("energyKcal", "")),
            protein=_to_float(record.get("protein", "")),
            fat=_to_float(record.get("fat", "")),
            carbohydrate=_to_float(record.get("carbohydrate", "")),
            sodium=_to_float(record.get("sodium", "")),
            calcium=_to_float(record.get("calcium", "")),
        ))

    if columns is None:
        raise ValueError("未找到营养表头(期望形如 [160]{productName,...}:)")
    if not rows:
        raise ValueError("营养表头存在但没有解析出任何数据行")
    return rows


def _clean_optional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    if not text or text.lower() == "null":
        return None
    return text


def _split_cells(line: str, expected: int) -> Optional[List[str]]:
    """按逗号切分,并把多余的碎片合并回 ``nutritionDescription`` 列。

    说明文本里可能出现逗号,因此当切片数量多于列数时,把中间多出来的部分
    拼回第二列(说明列),这样数值列仍然能对上。
    """
    parts = [p.strip() for p in line.split(",")]
    if len(parts) == expected:
        return parts
    if len(parts) > expected:
        # 列结构:name, description, 7 个数值列
        numeric_tail = parts[-(expected - 2):]
        head = parts[: len(parts) - (expected - 2)]
        name = head[0]
        description = ",".join(head[1:])
        merged = [name, description] + numeric_tail
        return merged if len(merged) == expected else None
    return None


class NutritionBook:
    """营养索引,支持按归一化名称精确/模糊匹配。"""

    def __init__(self, entries: Iterable[Nutrition]) -> None:
        self.entries: List[Nutrition] = list(entries)
        self._exact: Dict[str, Nutrition] = {}
        for entry in self.entries:
            # 先出现的优先,避免重复行覆盖(实测存在重复行)
            self._exact.setdefault(entry.key, entry)

    def __len__(self) -> int:
        return len(self.entries)

    @classmethod
    def from_raw(cls, raw: str) -> "NutritionBook":
        return cls(parse_nutrition_table(raw))

    @classmethod
    def from_entries(cls, entries: Sequence[Dict]) -> "NutritionBook":
        return cls(Nutrition(
            product_name=str(e.get("productName", "")),
            energy_kcal=e.get("energyKcal"),
            energy_kj=e.get("energyKj"),
            protein=e.get("protein"),
            fat=e.get("fat"),
            carbohydrate=e.get("carbohydrate"),
            sodium=e.get("sodium"),
            calcium=e.get("calcium"),
            description=e.get("description"),
        ) for e in entries)

    def get(self, name: str) -> Optional[Nutrition]:
        """精确匹配(归一化后)。"""
        return self._exact.get(normalize_name(name))

    def match(self, name: str, *, min_overlap: int = 3) -> Optional[Nutrition]:
        """先精确后模糊。

        模糊策略:取二者归一化名称的最长公共子串,长度 >= ``min_overlap`` 才认。
        这样可以命中 ``麦辣鸡腿汉堡(套餐)`` -> ``麦辣鸡腿汉堡``、
        ``中薯条(大)`` -> ``中薯条`` 这类带后缀的写法,同时避免把
        ``麦香鸡`` 错配到 ``麦香鱼``。
        """
        exact = self.get(name)
        if exact is not None:
            return exact
        target = normalize_name(name)
        if not target:
            return None
        best: Optional[Nutrition] = None
        best_len = 0
        for key, entry in self._exact.items():
            if key in target or target in key:
                overlap = min(len(key), len(target))
            else:
                overlap = _longest_common_substring(key, target)
            if overlap > best_len:
                best, best_len = entry, overlap
        return best if best_len >= min_overlap else None

    def coverage(self, names: Sequence[str]) -> Dict[str, object]:
        """统计匹配覆盖率,用于 ``doctor`` 自检与报告中的置信度提示。"""
        hit, miss = [], []
        for name in names:
            (hit if self.match(name) else miss).append(name)
        total = len(names) or 1
        return {
            "total": len(names),
            "matched": len(hit),
            "missing": miss,
            "rate": round(len(hit) / total, 4),
        }


def _longest_common_substring(a: str, b: str) -> int:
    if not a or not b:
        return 0
    previous = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        current = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                current[j] = previous[j - 1] + 1
                best = max(best, current[j])
        previous = current
    return best


# --------------------------------------------------------------- 套餐营养拆解
@dataclass
class ComboBreakdown:
    """套餐的营养拆解结果。

    ``query-meal-detail`` 会返回套餐的组成 ``rounds[].choices[]``,
    每个 choice 带 ``code`` / ``name`` / ``quantity`` / ``isDefault``。
    这些子项编码**不能**直接下单(实测 ``calculate-price`` 会回
    「商品已售罄或不可售」),但它们的**名称**能命中官方营养表,
    所以按名称逐个匹配再累加,即可把套餐的营养补出来。

    这是本项目提高营养覆盖率的关键手段:官方营养表只覆盖单品,
    套餐(菜单里约三成条目)本来完全没有营养数据。
    """

    combo_code: str
    combo_name: str
    nutrition: Nutrition
    matched: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        total = len(self.matched) + len(self.missing)
        return round(len(self.matched) / total, 4) if total else 0.0

    @property
    def complete(self) -> bool:
        return not self.missing

    def describe(self) -> str:
        parts = [f"{name}" for name in self.matched]
        text = " + ".join(parts) if parts else "无可匹配子项"
        if self.missing:
            text += f"(未匹配:{'、'.join(self.missing)})"
        return f"{self.combo_name} = {text}"

    def to_dict(self) -> Dict[str, object]:
        return {
            "comboCode": self.combo_code,
            "comboName": self.combo_name,
            "matched": list(self.matched),
            "missing": list(self.missing),
            "coverage": self.coverage,
            "nutrition": self.nutrition.to_dict(),
        }


def parse_meal_detail(data: Dict) -> List[Dict[str, object]]:
    """从 ``query-meal-detail`` 的 ``data`` 中抽出默认选中的子项。

    只取 ``quantity > 0`` 的项:同一 round 内 ``isDefault=1`` 但 ``quantity=0``
    表示「可选但未选中」(例如开心乐园餐里既列了纯牛奶也列了纯悦)。
    """
    children: List[Dict[str, object]] = []
    for round_info in (data or {}).get("rounds", []) or []:
        for choice in round_info.get("choices", []) or []:
            quantity = choice.get("quantity") or 0
            try:
                quantity = int(quantity)
            except (TypeError, ValueError):
                quantity = 0
            if quantity <= 0:
                continue
            children.append({
                "code": str(choice.get("code") or ""),
                "name": str(choice.get("name") or "").strip(),
                "quantity": quantity,
                "round": str(round_info.get("name") or ""),
            })
    return children


def breakdown_combo(data: Dict, book: "NutritionBook",
                    *, min_overlap: int = 3) -> ComboBreakdown:
    """把一个套餐的默认组成拆成营养合计。"""
    combo_code = str((data or {}).get("code") or "")
    combo_name = str((data or {}).get("name") or "").strip()
    totals = {"kj": 0.0, "kcal": 0.0, "protein": 0.0, "fat": 0.0,
              "carbohydrate": 0.0, "sodium": 0.0, "calcium": 0.0}
    matched: List[str] = []
    missing: List[str] = []

    for child in parse_meal_detail(data):
        entry = book.match(str(child["name"]), min_overlap=min_overlap)
        if entry is None:
            missing.append(f"{child['name']}×{child['quantity']}")
            continue
        factor = float(child["quantity"])
        matched.append(f"{entry.product_name}×{child['quantity']}")
        totals["kj"] += (entry.energy_kj or 0.0) * factor
        totals["kcal"] += (entry.energy_kcal or 0.0) * factor
        totals["protein"] += (entry.protein or 0.0) * factor
        totals["fat"] += (entry.fat or 0.0) * factor
        totals["carbohydrate"] += (entry.carbohydrate or 0.0) * factor
        totals["sodium"] += (entry.sodium or 0.0) * factor
        totals["calcium"] += (entry.calcium or 0.0) * factor

    nutrition = Nutrition(
        product_name=combo_name or combo_code,
        energy_kj=round(totals["kj"], 1) or None,
        energy_kcal=round(totals["kcal"], 1) or None,
        protein=round(totals["protein"], 1) or None,
        fat=round(totals["fat"], 1) or None,
        carbohydrate=round(totals["carbohydrate"], 1) or None,
        sodium=round(totals["sodium"], 1) or None,
        calcium=round(totals["calcium"], 1) or None,
        description="由 query-meal-detail 默认组成累加得出",
    )
    return ComboBreakdown(combo_code=combo_code, combo_name=combo_name,
                          nutrition=nutrition, matched=matched, missing=missing)
