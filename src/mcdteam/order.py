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
from dataclasses import dataclass, field, replace
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
    #: 外送/团餐的配送范围校验结果(到店场景为 applicable=False)
    delivery_check: Dict[str, Any] = field(default_factory=dict)
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
        detail = self.response.get("orderDetail")
        if isinstance(detail, dict) and detail.get("orderId"):
            return str(detail["orderId"])
        return ""

    @property
    def payment_url(self) -> str:
        """支付链接。

        实测字段名是 ``payH5Url``(形如
        ``https://m.mcd.cn/mcp/scanToPay?orderId=...``),
        不是直觉上的 ``payUrl``;这里同时兼容若干别名。
        """
        for key in ("payH5Url", "payUrl", "paymentUrl", "payLink", "h5Url",
                    "qrCodeUrl", "url"):
            value = self.response.get(key)
            if value and str(value).startswith("http"):
                return str(value)
        return ""

    @property
    def order_detail(self) -> Dict[str, Any]:
        detail = self.response.get("orderDetail")
        return detail if isinstance(detail, dict) else {}

    @property
    def order_status(self) -> str:
        return str(self.order_detail.get("orderStatus")
                   or self.response.get("orderStatus") or "")

    @property
    def expire_pay_time(self) -> str:
        """支付超时时间。实测下单后约 15 分钟。"""
        return str(self.order_detail.get("expirePayTime")
                   or self.response.get("expirePayTime") or "")

    @property
    def pickup_code(self) -> str:
        return str(self.order_detail.get("pickupCode")
                   or self.response.get("pickupCode") or "")

    @property
    def pay_id(self) -> str:
        return str(self.response.get("payId")
                   or self.order_detail.get("payId") or "")

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
        check = self.delivery_check or {}
        if check.get("applicable"):
            lines.append(f"配送校验    {'通过' if check.get('ok') else '未通过'}")
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
            "deliveryCheck": self.delivery_check or None,
            "takeWayCode": self.take_way_code or None,
            "notes": list(self.notes),
            "warnings": list(self.warnings),
            "orderId": self.order_id or None,
            "payId": self.pay_id or None,
            "paymentUrl": self.payment_url or None,
            "orderStatus": self.order_status or None,
            "expirePayTime": self.expire_pay_time or None,
            "pickupCode": self.pickup_code or None,
            "response": self.response or None,
            "error": self.error,
        }

    def payment_card(self) -> str:
        """一段可以直接发到群里的「订单 + 支付链接」文本。

        团餐的实际流程是:统筹者下单 → 把支付链接发到群里 → 大家各自付款或
        由财务统一支付。所以这段文本是必需的交付物,而不是装饰。
        """
        if not self.submitted:
            return ""
        name = self.store_context.store_name or self.store_context.store_code
        lines = [
            f"🍟 麦当劳团餐已下单 · {name}",
            f"👥 {self.store_context.mode.label}"
            + (f" · 预约 {self.store_context.reservation_date}"
               if self.store_context.reservation_date else ""),
            f"💰 应付 ¥{self.quoted_cents / 100:,.2f}",
            f"🧾 订单号 {self.order_id}",
        ]
        if self.expire_pay_time:
            lines.append(f"⏰ 请在 {self.expire_pay_time} 前完成支付,逾期订单自动关闭")
        if self.payment_url:
            lines.append(f"💳 支付链接 {self.payment_url}")
        else:
            lines.append("💳 请打开麦当劳 APP 在「我的订单」中完成支付")
        if self.pickup_code:
            lines.append(f"🔢 取餐码 {self.pickup_code}")
        return "\n".join(lines)


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

    # 外送/团餐:提前校验门店能否配送到该地址(实测 create-order 才会报
    # 「无法配送到当前地址」,拦在这里对用户更友好)
    check = validate_store_address(client, store_context)
    draft.delivery_check = check
    if check.get("applicable") and not check.get("ok"):
        draft.warnings.append(str(check.get("message") or "配送范围校验未通过"))

    draft.notes.append(
        "dry-run:未调用 create-order" if dry_run else "将真实调用 create-order"
    )
    return draft


def submit_order(client, draft: OrderDraft, *,
                 allow_real: bool = False,
                 allow_amount_mismatch: bool = False,
                 allow_delivery_mismatch: bool = False) -> OrderDraft:
    """提交订单。

    **只有同时满足下面几个条件才会真正下单**:

    1. ``draft.dry_run is False``(调用方显式关闭 dry-run)
    2. ``allow_real is True``(CLI 的 ``--pay``)
    3. 环境变量 ``MCD_ALLOW_REAL_ORDER=1``
    4. 配送范围校验通过(外送/团餐场景)

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

    check = draft.delivery_check or {}
    if check.get("applicable") and not check.get("ok") and not allow_delivery_mismatch:
        draft.warnings.append(
            "配送范围校验未通过,已中止下单(实测此时 create-order 会返回"
            "「无法配送到当前地址」)。请改用该地址可配送的门店,"
            "或显式允许 --allow-delivery-mismatch"
        )
        return draft

    if not draft.amount_ok and not allow_amount_mismatch:
        draft.warnings.append("金额超出容差且未允许差异,未下单(需要 --allow-amount-mismatch)")
        return draft

    try:
        draft.response = client.create_order(**draft.request)
        draft.submitted = True
        # 用订单返回的门店名回填上下文:到店自取路径未经过门店列表接口,
        # 拿不到 storeName,而订单详情里有权威名称
        detail = draft.order_detail
        if detail.get("storeName") and not draft.store_context.store_name:
            draft.store_context = replace(
                draft.store_context, store_name=str(detail["storeName"]))
        if detail.get("storeAddress"):
            draft.notes.append(f"门店地址:{detail['storeAddress']}")
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


#: ``query-order`` 返回的 ``status`` 码 → 文案(取自工具 outputSchema 的权威说明)
ORDER_STATUS_CODES = {
    "1": "待支付",
    "2": "配餐中-已支付",
    "4": "配送中",
    "6": "已完成",
    "7": "已取消",
    "8": "已评价",
    "10": "配餐中-餐厅确认配餐中",
}

#: 中文状态文案 → 语义分组。``order-list`` 只回中文文案,没有 status 码,
#: 所以判定必须走文案;这里同时兼容可能的措辞差异。
_STATUS_GROUPS = {
    "pending_payment": ("待支付", "未支付"),
    "paid": ("已支付", "配餐中", "制作中", "配送中", "待取餐"),
    "done": ("已完成", "已评价", "已取餐"),
    "cancelled": ("已取消", "已撤单", "已关闭", "已过期"),
}


def status_group(order_status: str) -> str:
    """把订单状态文案归到 ``pending_payment`` / ``paid`` / ``done`` / ``cancelled``。

    无法识别时返回 ``""`` —— 调用方应把未知状态当作「不动它」处理,而不是猜。
    """
    text = str(order_status or "").strip()
    if not text:
        return ""
    for group, keywords in _STATUS_GROUPS.items():
        if any(keyword in text for keyword in keywords):
            return group
    return ""


def is_pending_payment(order: Dict[str, Any]) -> bool:
    """该订单是否处于「待支付」状态。

    实测 ``order-list`` 会返回 ``orderStatus = "待支付"`` 的订单,所以可以用它
    找出所有被遗忘的未支付订单。
    """
    if not isinstance(order, dict):
        return False
    return status_group(order.get("orderStatus")) == "pending_payment"


def pending_payment_orders(client) -> List[Dict[str, Any]]:
    """挑出全部未支付订单,按创建时间倒序。"""
    orders = [o for o in client.order_list() if is_pending_payment(o)]
    orders.sort(key=lambda o: str(o.get("createTime") or ""), reverse=True)
    return orders


def summarise_order(order: Dict[str, Any]) -> str:
    """一行摘要,用于 ``order list`` 与 ``order cleanup`` 的表格。"""
    products = order.get("orderProductList") or []
    names = "、".join(
        f"{p.get('productName', '')}×{p.get('quantity', 1)}" for p in products[:3]
    )
    if len(products) > 3:
        names += f" 等 {len(products)} 项"
    amount = order.get("realTotalAmount") or order.get("totalAmount") or ""
    return (f"{order.get('orderStatus', ''):<10}"
            f" ¥{amount:<8} {names}")


# --------------------------------------------------------------- 支付后轮询
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_POLL_TIMEOUT = 180.0

#: 轮询停止条件:进入这些状态就没必要继续等了
TERMINAL_GROUPS = {"paid", "done", "cancelled"}


def wait_for_payment(client, order_id: str, *, timeout: float = DEFAULT_POLL_TIMEOUT,
                     interval: float = DEFAULT_POLL_INTERVAL,
                     progress=None,
                     sleep=None) -> Dict[str, Any]:
    """轮询订单状态,直到「已支付」或状态进入终态。

    为什么需要:``create-order`` 的描述里明确要求下单后引导用户
    「支付完成后回复'支付完成'或'支付失败',会帮您查询最新的订单状态」。
    人工来回问一次很费事,所以这里做一个有上限的轮询。

    返回::

        {"orderId":..., "orderStatus":..., "group":..., "polls": n,
         "timedOut": bool, "paid": bool, "cancelled": bool, "last": {...}}

    注意:轮询只调用只读的 ``query-order``,不会产生任何副作用。
    超时后会返回 ``timedOut=True`` 并把最后一次状态带回去,
    让调用方决定是继续等还是提示用户自己去查。
    """
    import time as _time

    sleeper = sleep or _time.sleep
    deadline = _time.monotonic() + max(0.0, float(timeout))
    polls = 0
    last: Dict[str, Any] = {}
    timed_out = False

    while True:
        polls += 1
        try:
            last = client.query_order(order_id)
        except MCPError as exc:
            return {"orderId": order_id, "orderStatus": "", "group": "", "polls": polls,
                    "timedOut": False, "paid": False, "cancelled": False,
                    "error": exc.message, "last": last}
        group = status_group(last.get("orderStatus"))
        if progress:
            progress(f"第 {polls} 次查询:{last.get('orderStatus', '未知')}")
        if group in TERMINAL_GROUPS:
            return {"orderId": order_id,
                    "orderStatus": str(last.get("orderStatus") or ""),
                    "group": group, "polls": polls, "timedOut": False,
                    "paid": group in ("paid", "done"),
                    "cancelled": group == "cancelled", "last": last}
        if _time.monotonic() >= deadline:
            timed_out = True
            return {"orderId": order_id,
                    "orderStatus": str(last.get("orderStatus") or ""),
                    "group": group, "polls": polls, "timedOut": True,
                    "paid": False, "cancelled": False, "last": last}
        sleeper(interval)


def validate_store_address(client, store_context: StoreContext) -> Dict[str, Any]:
    """外送/团餐下单前校验「门店能否配送到该地址」。

    **为什么需要这一步**(实测教训):门店(北京)与地址(无锡)不匹配时,
    ``query-meals`` / ``calculate-price`` / 请求装配**全部成功**,
    直到 ``create-order`` 才返回::

        无法配送到当前地址,请选择其他地址

    也就是说这个错误只在下单那一刻暴露 —— 对用户来说,方案都排好了、
    钱也算准了,最后一步才失败,体验很差。这里提前用
    ``delivery-query-stores`` 反查该地址的可配送门店,把问题拦在预览阶段。

    返回::

        {"applicable": bool, "ok": bool, "stores": [...], "storeCodes": [...],
         "message": str}

    ``applicable=False`` 表示该场景不适用(到店/得来速),此时不做判断。
    """
    if store_context.mode not in (FulfillmentMode.DELIVERY, FulfillmentMode.GROUP):
        return {"applicable": False, "ok": True, "stores": [], "storeCodes": [],
                "message": "到店/得来速场景不涉及配送地址校验"}
    if not store_context.address_id:
        return {"applicable": True, "ok": False, "stores": [], "storeCodes": [],
                "message": "缺少 addressId,无法校验配送范围"}

    try:
        stores = client.resolve_delivery_stores(
            store_context.address_id, be_type=store_context.be_type)
    except MCPError as exc:
        return {"applicable": True, "ok": True, "stores": [], "storeCodes": [],
                "message": f"配送范围校验跳过(接口不可用):{exc.message}"}
    except (AttributeError, TypeError):
        # 客户端未实现该方法(例如测试替身),跳过校验而不是让下单流程崩掉
        return {"applicable": True, "ok": True, "stores": [], "storeCodes": [],
                "message": "配送范围校验跳过(客户端不支持)"}

    codes = [str(item.get("storeCode", "")) for item in stores if item]
    ok = str(store_context.store_code) in codes
    message = ""
    if not ok:
        preview = "、".join(
            f"{item.get('storeName', '')}({item.get('storeCode', '')})"
            for item in stores[:5]
        )
        message = (
            f"门店 {store_context.store_code} 不在该地址的可配送门店列表中,"
            "`create-order` 会返回「无法配送到当前地址」。"
            + (f"该地址可配送的门店:{preview}" if preview else "")
        )
    return {"applicable": True, "ok": ok, "stores": stores,
            "storeCodes": codes, "message": message}


def describe_order(data: Dict[str, Any]) -> str:
    """把 ``query-order`` 的原始返回渲染成可读文本。

    实测返回结构(``data``)::

        {orderId, orderStatus, status, storeName, storeAddress,
         orderProductList[], totalAmount, realTotalAmount, totalDiscountAmount,
         couponList[], createTime, expirePayTime, takeWay, pickupCode, payId, remark}

    其中 ``orderProductList[].comboItemList[]`` 会给出套餐实际包含的单品
    —— 这点很有用:它反过来印证了「同一个编码在价格接口与详情接口叫法不同」
    的问题(实测编码 9900008747 在价格接口叫「牛肉堡单品精选」,
    而订单里显示它其实是个含「汉堡包」的套餐)。
    """
    if not isinstance(data, dict):
        return str(data)

    current = str(data.get("orderStatus") or "")
    code = str(data.get("status") or "")
    if not current:
        current = ORDER_STATUS_CODES.get(code, "未知状态")

    lines: List[str] = []
    lines.append(f"📦 订单 {data.get('orderId', '')}")
    lines.append(f"   状态        {current}" + (f"(status={code})" if code else ""))
    if data.get("storeName"):
        lines.append(f"   门店        {data['storeName']}")
    if data.get("storeAddress"):
        lines.append(f"   地址        {data['storeAddress']}")
    if data.get("takeWay"):
        lines.append(f"   取餐方式    {data['takeWay']}")
    if data.get("pickupCode"):
        lines.append(f"   取餐码      {data['pickupCode']}")
    if data.get("createTime"):
        lines.append(f"   下单时间    {data['createTime']}")
    if data.get("expirePayTime"):
        lines.append(f"   支付截止    {data['expirePayTime']}")

    products = data.get("orderProductList") or []
    if products:
        lines.append("   商品明细")
        for product in products:
            quantity = product.get("quantity", 1)
            price = product.get("price", "")
            lines.append(f"     · {product.get('productName', '')} ×{quantity}"
                         + (f"  ¥{price}" if price else ""))
            for child in product.get("comboItemList") or []:
                lines.append(f"         └ {child.get('itemName', '')}"
                             f" ×{child.get('itemQuantity', 1)}")

    if data.get("totalAmount") is not None:
        lines.append(f"   应付        ¥{data['totalAmount']}")
    if data.get("totalDiscountAmount") not in (None, "0", 0):
        lines.append(f"   优惠        -¥{data['totalDiscountAmount']}")
    if data.get("deliveryPrice") not in (None, "0", 0):
        lines.append(f"   配送费      ¥{data['deliveryPrice']}")
    if data.get("remark"):
        lines.append(f"   备注        {data['remark']}")

    if current == "待支付":
        lines.append("   ⏰ 尚未支付。逾期未支付订单会自动关闭,不会产生扣款。")
        lines.append("   ▶ 可执行:order cancel --order-id "
                     f"{data.get('orderId', '')} 撤销该订单")
        lines.append("   ▶ 支付后可用:order wait --order-id "
                     f"{data.get('orderId', '')} 自动确认状态")
    group = status_group(current)
    if group == "cancelled":
        lines.append("   ✔ 该订单已取消,不产生任何消费。")
    elif group in ("paid", "done"):
        lines.append("   ✔ 订单已支付,正在处理。")
        if data.get("pickupCode"):
            lines.append(f"   🔢 取餐码:{data['pickupCode']}")
        if data.get("lockerCode"):
            lines.append(f"   🔐 取餐柜:{data['lockerCode']}")
    return "\n".join(lines)
