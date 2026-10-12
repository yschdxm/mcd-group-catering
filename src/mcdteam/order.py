"""下单编排:把「方案」变成「订单」,并把风险关进闸门里。

## 为什么这个模块要写得这么谨慎

麦当劳 MCP **没有沙箱/测试环境**。实测确认:

* ``create-order`` / ``calculate-price`` / ``query-meals`` 的 schema 里
  **没有任何测试或 dry-run 标志**;
* 官方仓库全文搜索 ``sandbox`` / ``沙箱`` / ``测试环境`` 无任何命中;
* 调用 ``create-order`` 会**真实创建订单并返回支付链接**(``orderId`` + 支付链接)。

也就是说,一旦调用成功,就会真的产生一笔待支付订单。因此本模块的设计原则是
**默认什么都不做**:

## 三重闸门

1. **默认 dry-run**。``OrderDraft.dry_run=True`` 时只做装配与校验,
   绝不调用 ``create-order``。CLI 需要显式 ``--pay`` 才会关闭它。
2. **环境变量开关**。即使传了 ``--pay``,还要求环境变量
   ``MCD_ALLOW_REAL_ORDER=1`` 存在,防止误触。
3. **金额闸门**。提交前**重新核价**,若实付与服务端报价不一致且超出容差,
   直接中止并说明差异,而不是"照单提交"。

## 一个必须知道的坑

``takeWayCode`` 只在到店/得来速场景(``orderType=1``)必传,取值来自
``calculate-price`` 返回的 ``data.takeWayList[].code``(实测值形如
``eat-in`` / ``take-in-store``)。外送/团餐(``orderType=2``)不传该字段,
但**必传** ``addressId``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .mcp.context import FulfillmentMode, StoreContext
from .mcp.errors import MCPBusinessError, MCPError
from .solve.planner import CateringPlan

#: 允许真实下单的环境变量开关
REAL_ORDER_ENV = "MCD_ALLOW_REAL_ORDER"

#: 金额容差(分)。门店优惠随时可能变化,超过这个差异就要人工确认
AMOUNT_TOLERANCE_CENTS = 100

#: 取消原因码
CANCEL_REASONS = {
    1: "改主意了",
    2: "重复下单",
    3: "点错了/点多了/点少了",
    4: "地址/电话填错了",
    5: "送达时间选错了",
    -1: "其它原因",
}


class OrderSafetyError(RuntimeError):
    """下单前的安全闸门拦下了这次调用。"""


@dataclass
class OrderDraft:
    """一次下单的完整装配结果。"""

    store_context: StoreContext
    items: List[Dict[str, Any]]
    request: Dict[str, Any]
    quoted_cents: int = 0
    plan_payable_cents: int = 0
    take_way_options: List[Dict[str, Any]] = field(default_factory=list)
    take_way_code: str = ""
    dry_run: bool = True
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    submitted: bool = False
    response: Dict[str, Any] = field(default_factory=dict)
    error: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ 展示
    @property
    def amount_diff_cents(self) -> int:
        return self.quoted_cents - self.plan_payable_cents

    @property
    def amount_ok(self) -> bool:
        return abs(self.amount_diff_cents) <= AMOUNT_TOLERANCE_CENTS

    @property
    def order_id(self) -> str:
        for key in ("orderId", "orderNo", "orderNumber"):
            value = self.response.get(key)
            if value:
                return str(value)
        return ""

    @property
    def payment_url(self) -> str:
        for key in ("payUrl", "paymentUrl", "payLink", "url", "qrCodeUrl", "h5Url"):
            value = self.response.get(key)
            if value and str(value).startswith("http"):
                return str(value)
        return ""

    def summary(self) -> str:
        """一段可读的装配摘要,dry-run 时也能看清会提交什么。"""
        lines = [
            f"门店        {self.store_context.store_name or self.store_context.store_code}"
            f"({self.store_context.store_code})",
            f"取餐方式    {self.store_context.mode.label}"
            f"(orderType={self.store_context.order_type}, beType={self.store_context.be_type})",
        ]
        if self.store_context.be_code:
            lines.append(f"beCode      {self.store_context.be_code}")
        if self.store_context.gm_service_code:
            lines.append(f"助餐服务    {self.store_context.gm_service_code}")
        if self.store_context.reservation_date:
            lines.append(f"预约时间    {self.store_context.reservation_date}")
        if self.request.get("addressId"):
            lines.append(f"配送地址    {self.request['addressId']}")
        if self.take_way_code:
            lines.append(f"取餐方式码  {self.take_way_code}")
        if self.request.get("remark"):
            lines.append(f"备注        {self.request['remark']}")
        lines.append(f"商品件数    {sum(i['quantity'] for i in self.items)} 件")
        lines.append(f"服务端报价  ¥{self.quoted_cents / 100:,.2f}")
        lines.append(f"方案金额    ¥{self.plan_payable_cents / 100:,.2f}")
        if self.amount_diff_cents:
            lines.append(
                f"金额差异    {self.amount_diff_cents / 100:+,.2f} 元"
                f"({'在容差内' if self.amount_ok else '超出容差'})"
            )
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dryRun": self.dry_run,
            "submitted": self.submitted,
            "storeContext": self.store_context.to_dict(),
            "request": self.request,
            "quotedYuan": round(self.quoted_cents / 100.0, 2),
            "planPayableYuan": round(self.plan_payable_cents / 100.0, 2),
            "amountDiffYuan": round(self.amount_diff_cents / 100.0, 2),
            "amountOk": self.amount_ok,
            "takeWayOptions": self.take_way_options,
            "takeWayCode": self.take_way_code or None,
            "notes": list(self.notes),
            "warnings": list(self.warnings),
            "orderId": self.order_id or None,
            "paymentUrl": self.payment_url or None,
            "response": self.response or None,
            "error": self.error,
        }


def pick_take_way(take_way_list: Sequence[Dict[str, Any]],
                  preferred: str = "") -> tuple:
    """从 ``calculate-price`` 的 ``takeWayList`` 里选一个取餐方式码。

    返回 ``(code, options)``。``preferred`` 命中 ``code`` 或 ``title`` 时优先。
    """
    options = [dict(item) for item in (take_way_list or []) if isinstance(item, dict)]
    if not options:
        return "", []
    if preferred:
        for item in options:
            if preferred in (str(item.get("code", "")), str(item.get("title", ""))):
                return str(item.get("code", "")), options
    return str(options[0].get("code", "")), options


def build_order_draft(client, store_context: StoreContext, plan: CateringPlan, *,
                      take_way: str = "", remark: str = "",
                      need_tableware: Optional[bool] = None,
                      dry_run: bool = True,
                      progress=None) -> OrderDraft:
    """装配下单请求,并做提交前的全部校验(**不提交**)。

    步骤:

    1. 复算一次价格,拿到 ``takeWayList`` 与最新实付金额;
    2. 组装 ``items``(只带 ``productCode`` / ``quantity`` / 券);
    3. 按场景补齐 ``takeWayCode`` 或 ``addressId``;
    4. 比对金额差异并给出告警。
    """
    items = [{"productCode": line.item.code, "quantity": line.quantity}
             for line in plan.lines]
    if not items:
        raise OrderSafetyError("方案没有任何餐品,无法下单")

    draft = OrderDraft(
        store_context=store_context,
        items=items,
        request={},
        plan_payable_cents=plan.payable_cents,
        dry_run=dry_run,
    )

    if progress:
        progress("下单前重新核价")
    try:
        quote = client.calculate_price(
            store_context.store_code, items,
            order_type=store_context.order_type,
            be_type=store_context.be_type,
            be_code=store_context.be_code,
            reservation_date=store_context.reservation_date,
            gm_service_code=store_context.gm_service_code,
        )
    except MCPError as exc:
        raise OrderSafetyError(f"下单前核价失败,已中止:{exc.message}") from exc

    draft.quoted_cents = int(quote.get("price") or quote.get("productPrice") or 0)
    take_way_code, options = pick_take_way(quote.get("takeWayList") or [], take_way)
    draft.take_way_code = take_way_code
    draft.take_way_options = options

    if not draft.amount_ok:
        draft.warnings.append(
            f"服务端报价 ¥{draft.quoted_cents / 100:,.2f} 与方案金额 "
            f"¥{draft.plan_payable_cents / 100:,.2f} 相差 "
            f"¥{abs(draft.amount_diff_cents) / 100:,.2f},超出容差"
            f"(¥{AMOUNT_TOLERANCE_CENTS / 100:.2f});提交前请人工确认"
        )

    # 组装正式请求
    request: Dict[str, Any] = {
        "storeCode": store_context.store_code,
        "orderType": store_context.order_type,
        "beType": store_context.be_type,
        "items": items,
    }
    if store_context.be_code:
        request["beCode"] = store_context.be_code
    if store_context.reservation_date:
        request["reservationDate"] = store_context.reservation_date
    if store_context.gm_service_code:
        request["gmServiceCode"] = store_context.gm_service_code
    if store_context.order_type == 1:
        if not take_way_code:
            raise OrderSafetyError(
                "到店/得来速场景必须提供 takeWayCode,但 calculate-price 未返回 "
                "takeWayList;请检查门店与 orderType/beType 是否匹配"
            )
        request["takeWayCode"] = take_way_code
    else:
        address_id = store_context.address_id
        if not address_id:
            raise OrderSafetyError(
                f"{store_context.mode.label} 场景必须提供 addressId"
                "(来自 delivery-query-addresses)"
            )
        request["addressId"] = address_id
        if remark:
            request["remark"] = remark[:50]
    if need_tableware is not None:
        request["needTableware"] = bool(need_tableware)

    draft.request = request
    draft.notes.append(
        "dry-run:未调用 create-order" if dry_run else "将真实调用 create-order"
    )
    return draft


def submit_order(client, draft: OrderDraft, *,
                 allow_real: bool = False,
                 allow_amount_mismatch: bool = False) -> OrderDraft:
    """提交订单。

    **只有同时满足下面三个条件才会真正下单**:

    1. ``draft.dry_run is False``(调用方显式关闭 dry-run)
    2. ``allow_real is True``(CLI 的 ``--pay``)
    3. 环境变量 ``MCD_ALLOW_REAL_ORDER=1``

    另外,若金额超出容差且 ``allow_amount_mismatch`` 为 False,也会中止。
    任何一条不满足都只返回 draft 并写入 ``warnings``,不会产生副作用。
    """
    if draft.dry_run:
        draft.notes.append("dry-run 模式:仅装配请求,未下单")
        return draft

    if not allow_real:
        draft.warnings.append("缺少显式确认,未下单(需要 --pay)")
        return draft

    if os.environ.get(REAL_ORDER_ENV, "").strip() not in ("1", "true", "TRUE", "yes"):
        draft.warnings.append(
            f"未设置环境变量 {REAL_ORDER_ENV}=1,出于安全考虑未下单。"
            "麦当劳 MCP 没有测试环境,下单会产生真实待支付订单"
        )
        return draft

    if not draft.amount_ok and not allow_amount_mismatch:
        draft.warnings.append("金额超出容差且未允许差异,未下单(需要 --allow-amount-mismatch)")
        return draft

    try:
        draft.response = client.create_order(**draft.request)
        draft.submitted = True
        draft.notes.append("订单已创建,请通过支付链接或麦当劳 APP 完成支付")
    except MCPError as exc:
        draft.error = {
            "type": "create_order_failed",
            "message": exc.message,
            "tool": getattr(exc, "tool", "create-order"),
            "traceId": getattr(exc, "trace_id", None),
        }
        draft.warnings.append(f"下单失败:{exc.message}")
    return draft


def describe_cancel_reasons() -> str:
    return "、".join(f"{code}={text}" for code, text in sorted(CANCEL_REASONS.items()))
