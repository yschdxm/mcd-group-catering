"""优惠券与促销规则的归一化。

麦当劳 MCP 提供了三类「钱」相关的原料,形态各不相同:

``query-store-coupons``
    门店 + 取餐方式维度的可用券,结构规整::

        {"title": "中薯条买一送一", "couponId": "...", "couponCode": "...",
         "tradeDateTime": "2026-10-12 10:30:00-2026-10-16 23:59:59",
         "products": [{"productCode": "9900012387", "productName": "中薯条买一送一"}]}

    注意:这里的 ``products[].productCode`` **不是**能直接下单的餐品编码,
    而是券自身对应的商品编码;真正的用法是把 ``couponId``/``couponCode``
    挂到订单行上,由 ``calculate-price`` 计算优惠。

``query-my-coupons``
    会员卡包全量券,带 ``couponId``/``code``/``title``/``showType``。

``query-promotions``
    企业团餐专属的满减/满折规则(``promotionType`` 31=满额减、33=订单折扣;
    ``products.type`` 1=包含、2=不包含、3=全部)。规则原文交给求解器自行解读,
    工具本身不做最优推荐 —— 这正是本项目要补的能力。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

_MONEY_RE = re.compile(r"(?:满|消费满)\s*(\d+(?:\.\d+)?)\s*元?\s*(?:减|立减)\s*(\d+(?:\.\d+)?)")
_DISCOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*折")
#: 买赠券的份数既可能是阿拉伯数字也可能是中文数字(实测券面写的是「买一送一」)
_BOGO_RE = re.compile(
    r"买\s*(?P<buy>\d+|[一二两三四五六七八九十])\s*送\s*(?P<free>\d+|[一二两三四五六七八九十])")
_FREE_RE = re.compile(r"(?:送|赠|得)\s*(.+)$")

_CN_DIGITS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _digits(text: str) -> int:
    text = (text or "").strip()
    if text.isdigit():
        return int(text)
    return _CN_DIGITS.get(text, 0)


def _parse_time_range(text: str) -> tuple:
    """解析 ``2026-10-12 10:30:00-2026-10-16 23:59:59``。"""
    if not text or "-" not in text:
        return (None, None)
    parts = text.split("-")
    if len(parts) < 6:
        return (None, None)
    start_raw = "-".join(parts[:3]).strip()
    end_raw = "-".join(parts[3:]).strip()
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        return (datetime.strptime(start_raw, fmt), datetime.strptime(end_raw, fmt))
    except ValueError:
        return (None, None)


@dataclass
class Coupon:
    """归一化后的优惠券。"""

    coupon_id: str = ""
    coupon_code: str = ""
    title: str = ""
    kind: str = "unknown"      # bogo / threshold / discount / gift / unknown
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    threshold_yuan: float = 0.0
    discount_yuan: float = 0.0
    discount_rate: float = 0.0  # 0.85 表示 8.5 折
    buy: int = 0
    free: int = 0
    target_product: str = ""
    promotion_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_threshold(self) -> bool:
        return self.kind == "threshold"

    def is_active(self, moment: Optional[datetime] = None) -> bool:
        now = moment or datetime.now()
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_to and now > self.valid_to:
            return False
        return True

    def describe(self) -> str:
        window = ""
        if self.valid_to:
            window = f"(有效至 {self.valid_to:%m-%d %H:%M})"
        return f"{self.title}{window}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "couponId": self.coupon_id,
            "couponCode": self.coupon_code,
            "title": self.title,
            "kind": self.kind,
            "thresholdYuan": self.threshold_yuan,
            "discountYuan": self.discount_yuan,
            "discountRate": self.discount_rate,
            "buy": self.buy,
            "free": self.free,
            "validFrom": self.valid_from.isoformat() if self.valid_from else None,
            "validTo": self.valid_to.isoformat() if self.valid_to else None,
            "active": self.is_active(),
            "promotionId": self.promotion_id or None,
        }


def parse_coupon(record: Dict[str, Any]) -> Coupon:
    """把任意一种券记录解析为 :class:`Coupon`。"""
    title = str(record.get("title") or record.get("couponName") or "").strip()
    coupon = Coupon(
        coupon_id=str(record.get("couponId") or record.get("id") or ""),
        coupon_code=str(record.get("couponCode") or record.get("code") or ""),
        title=title,
        valid_from=None,
        valid_to=None,
        promotion_id=str(record.get("promotionId") or ""),
        raw=record,
    )
    coupon.valid_from, coupon.valid_to = _parse_time_range(str(record.get("tradeDateTime") or ""))

    products = record.get("products") or []
    if products and isinstance(products, list):
        first = products[0] or {}
        coupon.target_product = str(first.get("productName") or first.get("productCode") or "")

    match = _MONEY_RE.search(title)
    if match:
        coupon.kind = "threshold"
        coupon.threshold_yuan = float(match.group(1))
        coupon.discount_yuan = float(match.group(2))
        return coupon

    match = _DISCOUNT_RE.search(title)
    if match:
        coupon.kind = "discount"
        coupon.discount_rate = float(match.group(1)) / 10.0
        return coupon

    match = _BOGO_RE.search(title)
    if match:
        coupon.kind = "bogo"
        coupon.buy = _digits(match.group("buy"))
        coupon.free = _digits(match.group("free"))
        return coupon

    if _FREE_RE.search(title) or "送" in title or "赠" in title:
        coupon.kind = "gift"
        return coupon

    return coupon


def parse_coupons(records: Iterable[Dict[str, Any]]) -> List[Coupon]:
    out: List[Coupon] = []
    seen = set()
    for record in records or []:
        coupon = parse_coupon(record)
        key = (coupon.coupon_id, coupon.coupon_code, coupon.title)
        if key in seen:
            continue
        seen.add(key)
        out.append(coupon)
    return out


@dataclass
class Promotion:
    """企业团餐促销规则(``query-promotions`` 原始结构的语义化视图)。"""

    promotion_id: str
    promotion_type: int
    rule_category: int
    include_all: bool = False
    include_products: List[str] = field(default_factory=list)
    exclude_products: List[str] = field(default_factory=list)
    threshold_yuan: float = 0.0
    discount_yuan: float = 0.0
    discount_rate: float = 0.0
    start_time: str = ""
    end_time: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.threshold_yuan and self.discount_yuan:
            return f"满 {self.threshold_yuan:g} 元减 {self.discount_yuan:g} 元"
        if self.discount_rate:
            return f"{self.discount_rate * 10:g} 折"
        return f"促销规则 {self.promotion_id}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "promotionId": self.promotion_id,
            "promotionType": self.promotion_type,
            "ruleCategory": self.rule_category,
            "label": self.label,
            "includeAll": self.include_all,
            "includeProducts": list(self.include_products),
            "excludeProducts": list(self.exclude_products),
            "thresholdYuan": self.threshold_yuan,
            "discountYuan": self.discount_yuan,
            "discountRate": self.discount_rate,
            "startTime": self.start_time,
            "endTime": self.end_time,
        }


def parse_promotions(records: Iterable[Dict[str, Any]]) -> List[Promotion]:
    """把 ``query-promotions`` 的原始规则翻译成可判定的满减/折扣对象。

    字段含义(来自工具描述):
    ``promotionType`` 31=满额减、33=订单折扣;
    ``ruleCategory`` 30=满折/折扣、40=满减;
    ``products.type`` 1=包含、2=不包含、3=全部。
    """
    out: List[Promotion] = []
    for record in records or []:
        products = _as_list(record.get("products"))
        include, exclude, include_all = [], [], False
        for entry in products:
            if not isinstance(entry, dict):
                continue
            ptype = _as_int(entry.get("type"), 0)
            code = str(entry.get("productCode") or "")
            if ptype == 3:
                include_all = True
            elif ptype == 1 and code:
                include.append(code)
            elif ptype == 2 and code:
                exclude.append(code)

        detail = record.get("ruleDetail") or {}
        if isinstance(detail, list):
            detail = detail[0] if detail else {}
        if not isinstance(detail, dict):
            detail = {}

        promotion = Promotion(
            promotion_id=str(record.get("promotionId") or ""),
            promotion_type=_as_int(record.get("promotionType"), 0),
            rule_category=_as_int(record.get("ruleCategory"), 0),
            include_all=include_all,
            include_products=include,
            exclude_products=exclude,
            start_time=str(record.get("startTime") or ""),
            end_time=str(record.get("endTime") or ""),
            raw=record,
        )
        promotion.threshold_yuan, promotion.discount_yuan, promotion.discount_rate = \
            _extract_rule_numbers(detail, promotion)
        out.append(promotion)
    return out


def _extract_rule_numbers(detail: Dict[str, Any], promotion: Promotion) -> tuple:
    """从 ``ruleDetail`` 中尽量抽取门槛/优惠额度。

    服务端返回的是原始结构且字段名未公开文档化,因此这里做宽口径扫描:
    按关键字匹配 + 数值量纲猜测(>100 视为「分」,否则视为「元」)。
    """
    threshold = discount = rate = 0.0
    for key, value in _flatten(detail):
        lowered = key.lower()
        number = _as_float(value)
        if number is None:
            continue
        yuan = number / 100.0 if number > 100 else number
        if any(k in lowered for k in ("threshold", "condition", "limit", "满")):
            threshold = max(threshold, yuan)
        elif any(k in lowered for k in ("discountamount", "reduceamount", "minus", "减")):
            discount = max(discount, yuan)
        elif any(k in lowered for k in ("discountrate", "rate", "zhe", "折")):
            candidate = number / 100.0 if number > 10 else number / 10.0
            if 0.05 <= candidate < 1.0:
                rate = max(rate, candidate)

    if promotion.promotion_type == 31 and not (threshold and discount):
        numbers = sorted(
            (v / 100.0 if v > 100 else v)
            for _, v in _flatten(detail) if _as_float(v) is not None
        )
        if len(numbers) >= 2:
            threshold, discount = numbers[-2], numbers[-1]
    return threshold, discount, rate


def _flatten(data: Any, prefix: str = "") -> List[tuple]:
    out: List[tuple] = []
    if isinstance(data, dict):
        for key, value in data.items():
            out.extend(_flatten(value, f"{prefix}{key}"))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            out.extend(_flatten(value, f"{prefix}[{index}]"))
    else:
        out.append((prefix, data))
    return out


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None
