"""门店上下文:把「去哪家店、怎么取、什么时候要」收敛成一个不可变对象。

麦当劳 MCP 的门店级工具有一条很容易踩错的传参规则链:

======================  ===========  ==========  ==================
取餐方式                 orderType    beType      beCode
======================  ===========  ==========  ==================
到店自取(堂食/外带)        1            1           不传
得来速车道取餐             1            5           必传
麦乐送到家                 2            2           必传
企业团餐                   2            6           必传
======================  ===========  ==========  ==================

``beCode`` 来自 ``query-nearby-stores``(得来速)或 ``delivery-query-stores``
(麦乐送/团餐),忘记传就会被服务端以 ``缺少参数`` 拒绝。
本模块把这条规则编码成枚举,避免调用点各写一遍。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, Optional


class FulfillmentMode(str, Enum):
    """取餐 / 配送方式。"""

    PICKUP = "pickup"          # 到店自取
    DRIVE_THRU = "drive_thru"  # 得来速车道取餐
    DELIVERY = "delivery"      # 麦乐送
    GROUP = "group"            # 企业团餐

    @property
    def order_type(self) -> int:
        return 1 if self in (FulfillmentMode.PICKUP, FulfillmentMode.DRIVE_THRU) else 2

    @property
    def be_type(self) -> int:
        return {
            FulfillmentMode.PICKUP: 1,
            FulfillmentMode.DRIVE_THRU: 5,
            FulfillmentMode.DELIVERY: 2,
            FulfillmentMode.GROUP: 6,
        }[self]

    @property
    def requires_be_code(self) -> bool:
        return self is not FulfillmentMode.PICKUP

    @property
    def label(self) -> str:
        return {
            FulfillmentMode.PICKUP: "到店自取",
            FulfillmentMode.DRIVE_THRU: "得来速车道取餐",
            FulfillmentMode.DELIVERY: "麦乐送",
            FulfillmentMode.GROUP: "企业团餐",
        }[self]

    @classmethod
    def parse(cls, value: Any) -> "FulfillmentMode":
        if isinstance(value, FulfillmentMode):
            return value
        text = str(value or "").strip().lower()
        aliases = {
            "pickup": cls.PICKUP, "take-out": cls.PICKUP, "togo": cls.PICKUP,
            "到店": cls.PICKUP, "自取": cls.PICKUP, "堂食": cls.PICKUP, "外带": cls.PICKUP,
            "1": cls.PICKUP,
            "drive": cls.DRIVE_THRU, "dt": cls.DRIVE_THRU, "得来速": cls.DRIVE_THRU,
            "车道": cls.DRIVE_THRU, "5": cls.DRIVE_THRU,
            "delivery": cls.DELIVERY, "mcdelivery": cls.DELIVERY, "外送": cls.DELIVERY,
            "麦乐送": cls.DELIVERY, "2": cls.DELIVERY,
            "group": cls.GROUP, "catering": cls.GROUP, "团餐": cls.GROUP,
            "企业团餐": cls.GROUP, "6": cls.GROUP,
        }
        if text in aliases:
            return aliases[text]
        raise ValueError(f"无法识别的取餐方式:{value!r}(可选 pickup/drive_thru/delivery/group)")


@dataclass(frozen=True)
class StoreContext:
    """一次统筹所使用的门店与履约上下文。"""

    store_code: str
    store_name: str = ""
    mode: FulfillmentMode = FulfillmentMode.PICKUP
    be_code: str = ""
    address_id: str = ""
    reservation_date: str = ""
    city: str = ""
    #: 企业团餐的助餐服务码(来自 query-meal-assistance)。
    #: **务必传**:实测团餐场景下 calculate-price / create-order 缺少它会被拒绝,
    #: 错误文案是「团餐助餐服务参数缺失」。
    gm_service_code: str = ""

    def __post_init__(self) -> None:
        if not str(self.store_code or "").strip():
            raise ValueError("storeCode 不能为空")
        if self.mode.requires_be_code and not self.be_code:
            raise ValueError(
                f"{self.mode.label} 场景必须提供 beCode"
                "(来自 query-nearby-stores 或 delivery-query-stores)"
            )

    @property
    def order_type(self) -> int:
        return self.mode.order_type

    @property
    def be_type(self) -> int:
        return self.mode.be_type

    def store_arguments(self) -> Dict[str, Any]:
        """生成门店级工具通用参数片段。"""
        arguments: Dict[str, Any] = {
            "storeCode": self.store_code,
            "orderType": self.order_type,
            "beType": self.be_type,
        }
        if self.be_code and self.mode.requires_be_code:
            arguments["beCode"] = self.be_code
        if self.reservation_date:
            arguments["reservationDate"] = self.reservation_date
        return arguments

    def to_dict(self) -> Dict[str, Any]:
        return {
            "storeCode": self.store_code,
            "storeName": self.store_name,
            "mode": self.mode.value,
            "orderType": self.order_type,
            "beType": self.be_type,
            "beCode": self.be_code or None,
            "gmServiceCode": self.gm_service_code or None,
            "reservationDate": self.reservation_date or None,
            "addressId": self.address_id or None,
            "city": self.city or None,
        }

    def with_reservation(self, reservation_date: str) -> "StoreContext":
        return replace(self, reservation_date=reservation_date)

    def with_gm_service(self, gm_service_code: str) -> "StoreContext":
        return replace(self, gm_service_code=gm_service_code)

    def price_arguments(self) -> Dict[str, Any]:
        """生成价格/下单类工具的完整参数(含团餐助餐服务码)。"""
        arguments = self.store_arguments()
        if self.gm_service_code:
            arguments["gmServiceCode"] = self.gm_service_code
        return arguments

    @classmethod
    def from_store_record(cls, record: Dict[str, Any], mode: FulfillmentMode,
                          reservation_date: str = "") -> "StoreContext":
        return cls(
            store_code=str(record.get("storeCode", "")),
            store_name=str(record.get("storeName", "") or ""),
            mode=mode,
            be_code=str(record.get("beCode", "") or ""),
            reservation_date=reservation_date,
        )

    def describe(self) -> str:
        bits = [f"门店 {self.store_name or self.store_code}({self.store_code})", self.mode.label]
        if self.be_code:
            bits.append(f"beCode={self.be_code}")
        if self.gm_service_code:
            bits.append(f"助餐={self.gm_service_code}")
        if self.reservation_date:
            bits.append(f"预约 {self.reservation_date}")
        return " · ".join(bits)


def resolve_store_record(records: list, store_code: str) -> Optional[Dict[str, Any]]:
    """在门店列表中按 storeCode 精确匹配(便于 CLI 复用最近一次查询结果)。"""
    for record in records or []:
        if str(record.get("storeCode")) == str(store_code):
            return record
    return None
