"""下单模块测试。

⚠️ **本文件永不调用真实的 ``create-order``。**

原因:麦当劳 MCP **没有沙箱/测试环境** —— ``create-order`` 一旦成功就会产生
真实的待支付订单。因此这里只测三件事:

1. **参数装配**是否正确(``takeWayCode`` / ``addressId`` / ``gmServiceCode`` 的传参规则);
2. **三重安全闸门**是否真的拦得住(dry-run、``--pay``、环境变量);
3. **金额闸门**是否在差异超容差时中止。

所有 MCP 交互都用假客户端(FakeClient)替代,不会有任何网络副作用。
"""

from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from mcdteam.data.menu import MenuCatalog, MenuItem, Role  # noqa: E402
from mcdteam.mcp.context import FulfillmentMode, StoreContext  # noqa: E402
from mcdteam.mcp.errors import MCPBusinessError  # noqa: E402
from mcdteam.order import (  # noqa: E402
    AMOUNT_TOLERANCE_CENTS,
    CANCEL_REASONS,
    REAL_ORDER_ENV,
    OrderSafetyError,
    build_order_draft,
    describe_cancel_reasons,
    describe_order,
    is_pending_payment,
    ORDER_STATUS_CODES,
    pending_payment_orders,
    status_group,
    summarise_order,
    wait_for_payment,
    pick_take_way,
    submit_order,
    validate_store_address,
)
from mcdteam.solve.constraints import CateringRequirements  # noqa: E402
from mcdteam.solve.planner import CateringPlanner  # noqa: E402


# --------------------------------------------------------------------- 假客户端
class FakeClient:
    """记录调用参数、返回可控报价的假客户端。"""

    def __init__(self, price_cents: int = 5000, take_way_list=None,
                 delivery_stores=None):
        self.price_cents = price_cents
        self.take_way_list = take_way_list if take_way_list is not None else [
            {"code": "eat-in", "title": "堂食", "subtitle": "店内用餐"},
            {"code": "take-in-store", "title": "外带", "subtitle": "店内自提"},
        ]
        #: 该地址的可配送门店;None 表示"客户端不支持校验"(模拟旧替身)
        self.delivery_stores = delivery_stores
        self.create_calls = []
        self.price_calls = []
        self.delivery_calls = []
        self.fail_create = None
        self.fail_price = None

    def resolve_delivery_stores(self, address_id, be_type=6):
        self.delivery_calls.append({"addressId": address_id, "beType": be_type})
        if self.delivery_stores is None:
            raise AttributeError("resolve_delivery_stores")
        return list(self.delivery_stores)

    def calculate_price(self, store_code, items, **kwargs):
        self.price_calls.append({"storeCode": store_code, "items": items, **kwargs})
        if self.fail_price:
            raise self.fail_price
        return {
            "price": self.price_cents,
            "originalPrice": self.price_cents,
            "discount": 0,
            "takeWayList": self.take_way_list,
        }

    def create_order(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.fail_create:
            raise self.fail_create
        return {"orderId": "TEST-ORDER-0001",
                "payH5Url": "https://example.invalid/pay/test",
                "orderDetail": {"orderId": "TEST-ORDER-0001", "orderStatus": "待支付",
                                "storeName": "测试门店"}}


def _plan(headcount: int = 2, budget: int = 100):
    catalog = MenuCatalog([
        MenuItem(code="1100", name="巨无霸", price=2550, role=Role.MAIN),
        MenuItem(code="3000", name="中杯可乐", price=1000, role=Role.DRINK),
    ], store_code="1950564", store_name="测试门店")
    requirements = CateringRequirements(headcount=headcount, budget_total=budget, effort=8)
    return CateringPlanner(catalog, requirements).solve()


def _pickup_context() -> StoreContext:
    return StoreContext(store_code="1950564", store_name="测试门店",
                        mode=FulfillmentMode.PICKUP)


def _delivery_context() -> StoreContext:
    return StoreContext(store_code="1950564", store_name="测试门店",
                        mode=FulfillmentMode.DELIVERY, be_code="BE001",
                        address_id="ADDR001")


def _group_context() -> StoreContext:
    return StoreContext(store_code="1950564", mode=FulfillmentMode.GROUP,
                        be_code="BE001", address_id="ADDR001",
                        gm_service_code="GMS001")


# --------------------------------------------------------------- takeWay 选择
def test_pick_take_way_defaults_to_first():
    code, options = pick_take_way([{"code": "eat-in"}, {"code": "take-in-store"}])
    assert code == "eat-in"
    assert len(options) == 2


def test_pick_take_way_prefers_match_by_code_or_title():
    assert pick_take_way([{"code": "eat-in", "title": "堂食"},
                          {"code": "take-in-store", "title": "外带"}],
                         "take-in-store")[0] == "take-in-store"
    assert pick_take_way([{"code": "eat-in", "title": "堂食"},
                          {"code": "take-in-store", "title": "外带"}],
                         "外带")[0] == "take-in-store"


def test_pick_take_way_empty_list():
    assert pick_take_way([]) == ("", [])
    assert pick_take_way(None) == ("", [])


# ------------------------------------------------------------------- 参数装配
def test_draft_pickup_requires_take_way_code():
    client = FakeClient(price_cents=7000)
    draft = build_order_draft(client, _pickup_context(), _plan())
    assert draft.request["takeWayCode"] == "eat-in"
    assert "addressId" not in draft.request
    assert "beCode" not in draft.request          # 到店自取不传 beCode
    assert draft.request["orderType"] == 1
    assert draft.request["beType"] == 1


def test_draft_delivery_requires_address_and_be_code():
    client = FakeClient(price_cents=7000)
    draft = build_order_draft(client, _delivery_context(), _plan())
    assert draft.request["addressId"] == "ADDR001"
    assert draft.request["beCode"] == "BE001"
    assert "takeWayCode" not in draft.request      # 外送不传 takeWayCode
    assert draft.request["orderType"] == 2


def test_draft_group_carries_gm_service_code():
    client = FakeClient(price_cents=7000)
    draft = build_order_draft(client, _group_context(), _plan())
    assert draft.request["gmServiceCode"] == "GMS001"
    assert draft.request["beType"] == 6
    assert draft.request["addressId"] == "ADDR001"


def test_draft_pickup_without_take_way_list_is_blocked():
    """calculate-price 没返回 takeWayList 时必须中止,而不是发出缺参请求。"""
    client = FakeClient(price_cents=7000, take_way_list=[])
    with pytest.raises(OrderSafetyError) as excinfo:
        build_order_draft(client, _pickup_context(), _plan())
    assert "takeWayCode" in str(excinfo.value)


def test_draft_delivery_without_address_is_blocked():
    context = StoreContext(store_code="1950564", mode=FulfillmentMode.DELIVERY,
                           be_code="BE001")
    with pytest.raises(OrderSafetyError) as excinfo:
        build_order_draft(FakeClient(), context, _plan())
    assert "addressId" in str(excinfo.value)


def test_draft_rejects_empty_plan():
    empty = _plan()
    empty.lines = []
    with pytest.raises(OrderSafetyError):
        build_order_draft(FakeClient(), _pickup_context(), empty)


def test_draft_passes_remark_and_tableware_for_delivery():
    draft = build_order_draft(FakeClient(), _delivery_context(), _plan(),
                              remark="无接触配送", need_tableware=False)
    assert draft.request["remark"] == "无接触配送"
    assert draft.request["needTableware"] is False


def test_draft_truncates_long_remark():
    draft = build_order_draft(FakeClient(), _delivery_context(), _plan(),
                              remark="备" * 80)
    assert len(draft.request["remark"]) == 50


def test_draft_items_only_contain_product_code_and_quantity():
    client = FakeClient(price_cents=7000)
    draft = build_order_draft(client, _pickup_context(), _plan())
    for item in draft.request["items"]:
        assert set(item.keys()) == {"productCode", "quantity"}
        assert isinstance(item["quantity"], int) and item["quantity"] >= 1


def test_draft_price_failure_is_wrapped_as_safety_error():
    client = FakeClient()
    client.fail_price = MCPBusinessError("缺少参数", tool="calculate-price")
    with pytest.raises(OrderSafetyError) as excinfo:
        build_order_draft(client, _pickup_context(), _plan())
    assert "核价失败" in str(excinfo.value)


# ------------------------------------------------------------------- 安全闸门
def test_dry_run_never_calls_create_order():
    client = FakeClient(price_cents=7000)
    draft = build_order_draft(client, _pickup_context(), _plan(), dry_run=True)
    draft = submit_order(client, draft, allow_real=True)
    assert client.create_calls == [], "dry-run 绝不能调用 create-order"
    assert draft.submitted is False
    assert draft.request, "请求仍应装配好,便于人工核对"


def test_real_order_blocked_without_explicit_allow():
    client = FakeClient(price_cents=7000)
    draft = build_order_draft(client, _pickup_context(), _plan(), dry_run=False)
    draft = submit_order(client, draft, allow_real=False)
    assert client.create_calls == []
    assert draft.submitted is False
    assert any("--pay" in w for w in draft.warnings)


def test_real_order_blocked_without_env_gate(monkeypatch):
    monkeypatch.delenv(REAL_ORDER_ENV, raising=False)
    client = FakeClient(price_cents=7000)
    draft = build_order_draft(client, _pickup_context(), _plan(), dry_run=False)
    draft = submit_order(client, draft, allow_real=True)
    assert client.create_calls == [], "缺少环境变量闸门时绝不能下单"
    assert draft.submitted is False
    assert any(REAL_ORDER_ENV in w for w in draft.warnings)


def test_real_order_runs_only_when_all_gates_open(monkeypatch):
    """三重闸门全开时才允许调用 —— 用假客户端验证调用参数,不产生真实订单。"""
    monkeypatch.setenv(REAL_ORDER_ENV, "1")
    plan = _plan()
    # 报价贴近方案金额,确保被拦下的原因是闸门而不是金额容差
    client = FakeClient(price_cents=plan.payable_cents)
    draft = build_order_draft(client, _pickup_context(), plan, dry_run=False)
    assert draft.amount_ok, draft.warnings
    draft = submit_order(client, draft, allow_real=True)
    assert len(client.create_calls) == 1
    assert draft.submitted is True
    assert draft.order_id == "TEST-ORDER-0001"
    assert draft.payment_url == "https://example.invalid/pay/test"
    sent = client.create_calls[0]
    assert sent["storeCode"] == "1950564"
    assert sent["takeWayCode"] == "eat-in"
    assert sent["items"]


# ------------------------------------------------------------------- 金额闸门
def test_amount_within_tolerance_is_accepted():
    plan = _plan()
    client = FakeClient(price_cents=plan.payable_cents + 50)
    draft = build_order_draft(client, _pickup_context(), plan)
    assert draft.amount_ok is True
    assert not draft.warnings


def test_amount_beyond_tolerance_blocks_submission(monkeypatch):
    monkeypatch.setenv(REAL_ORDER_ENV, "1")
    plan = _plan()
    client = FakeClient(price_cents=plan.payable_cents + AMOUNT_TOLERANCE_CENTS + 500)
    draft = build_order_draft(client, _pickup_context(), plan, dry_run=False)
    assert draft.amount_ok is False
    assert any("超出容差" in w for w in draft.warnings)

    draft = submit_order(client, draft, allow_real=True)
    assert client.create_calls == [], "金额超容差时默认不得下单"
    assert any("金额超出容差" in w for w in draft.warnings)


def test_amount_mismatch_can_be_overridden(monkeypatch):
    monkeypatch.setenv(REAL_ORDER_ENV, "1")
    plan = _plan()
    client = FakeClient(price_cents=plan.payable_cents + AMOUNT_TOLERANCE_CENTS + 500)
    draft = build_order_draft(client, _pickup_context(), plan, dry_run=False)
    draft = submit_order(client, draft, allow_real=True, allow_amount_mismatch=True)
    assert len(client.create_calls) == 1


def test_create_order_failure_is_captured_not_raised(monkeypatch):
    monkeypatch.setenv(REAL_ORDER_ENV, "1")
    plan = _plan()
    client = FakeClient(price_cents=plan.payable_cents)
    client.fail_create = MCPBusinessError("商品已售罄", tool="create-order",
                                          trace_id="trace-xyz")
    draft = build_order_draft(client, _pickup_context(), plan, dry_run=False)
    draft = submit_order(client, draft, allow_real=True)
    assert draft.submitted is False
    assert draft.error and draft.error["type"] == "create_order_failed"
    assert draft.error["traceId"] == "trace-xyz"


# ------------------------------------------------------------------- 展示辅助
def test_draft_summary_and_dict_are_serialisable():
    import json
    draft = build_order_draft(FakeClient(price_cents=7000), _pickup_context(), _plan())
    text = draft.summary()
    assert "门店" in text and "取餐方式" in text and "服务端报价" in text
    payload = json.dumps(draft.to_dict(), ensure_ascii=False)
    assert "takeWayCode" in payload


def test_cancel_reasons_documented():
    assert CANCEL_REASONS[1] == "改主意了"
    assert "-1=其它原因" in describe_cancel_reasons()


# ------------------------------------------------------------- 配送范围提前校验
def test_delivery_check_skipped_for_pickup():
    """到店场景不做配送校验。"""
    client = FakeClient(price_cents=7000)
    draft = build_order_draft(client, _pickup_context(), _plan())
    assert draft.delivery_check["applicable"] is False
    assert client.delivery_calls == []


def test_delivery_check_passes_when_store_in_range(monkeypatch):
    monkeypatch.setenv(REAL_ORDER_ENV, "1")
    plan = _plan()
    client = FakeClient(price_cents=plan.payable_cents,
                        delivery_stores=[{"storeCode": "1950564", "storeName": "测试门店"}])
    draft = build_order_draft(client, _delivery_context(), plan, dry_run=False)
    assert draft.delivery_check["ok"] is True
    assert not any("可配送门店列表" in w for w in draft.warnings)
    draft = submit_order(client, draft, allow_real=True)
    assert len(client.create_calls) == 1, "校验通过时应当允许下单"


def test_delivery_check_blocks_when_store_out_of_range(monkeypatch):
    """实测:门店与地址不匹配时 create-order 才报「无法配送到当前地址」。

    这里验证我们在预览阶段就拦下,并且在提交时默认拒绝。
    """
    monkeypatch.setenv(REAL_ORDER_ENV, "1")
    plan = _plan()
    client = FakeClient(
        price_cents=plan.payable_cents,
        delivery_stores=[{"storeCode": "9999999", "storeName": "别的门店"}],
    )
    draft = build_order_draft(client, _delivery_context(), plan, dry_run=False)
    assert draft.delivery_check["ok"] is False
    assert any("不在该地址的可配送门店列表" in w for w in draft.warnings)
    assert "别的门店" in draft.warnings[0], "应把可用门店告知用户"

    draft = submit_order(client, draft, allow_real=True)
    assert client.create_calls == [], "配送范围不符时不得下单"
    assert any("配送范围校验未通过" in w for w in draft.warnings)


def test_delivery_check_can_be_overridden(monkeypatch):
    monkeypatch.setenv(REAL_ORDER_ENV, "1")
    plan = _plan()
    client = FakeClient(price_cents=plan.payable_cents, delivery_stores=[])
    draft = build_order_draft(client, _delivery_context(), plan, dry_run=False)
    draft = submit_order(client, draft, allow_real=True, allow_delivery_mismatch=True)
    assert len(client.create_calls) == 1


def test_delivery_check_tolerates_client_without_support():
    """替身客户端没有该方法时,校验应跳过而不是让下单流程崩掉。"""
    class MinimalClient(FakeClient):
        resolve_delivery_stores = None

    plan = _plan()
    client = FakeClient(price_cents=plan.payable_cents)
    client.delivery_stores = None      # 触发内部 AttributeError 分支
    draft = build_order_draft(client, _delivery_context(), plan)
    assert draft.delivery_check["ok"] is True
    assert "跳过" in draft.delivery_check["message"]


# --------------------------------------------------------- 支付链接与支付卡片
def test_payment_url_reads_pay_h5_url():
    """实测字段名是 payH5Url,不是 payUrl。"""
    draft = build_order_draft(FakeClient(), _pickup_context(), _plan(), dry_run=False)
    draft.submitted = True
    draft.response = {"orderId": "X1",
                      "payH5Url": "https://m.mcd.cn/mcp/scanToPay?orderId=X1"}
    assert draft.payment_url.startswith("https://m.mcd.cn/mcp/scanToPay")
    assert draft.order_id == "X1"


def test_order_detail_helpers():
    draft = build_order_draft(FakeClient(), _pickup_context(), _plan(), dry_run=False)
    draft.response = {
        "orderId": "X2", "payId": "P2",
        "orderDetail": {"orderStatus": "待支付", "expirePayTime": "2026-10-12 18:45:10",
                        "pickupCode": "A123", "storeName": "某门店"},
    }
    assert draft.order_status == "待支付"
    assert draft.expire_pay_time == "2026-10-12 18:45:10"
    assert draft.pickup_code == "A123"
    assert draft.pay_id == "P2"


def test_payment_card_contains_link_and_deadline():
    draft = build_order_draft(FakeClient(), _pickup_context(), _plan(), dry_run=False)
    draft.submitted = True
    draft.quoted_cents = 4000
    draft.response = {"orderId": "X3",
                      "payH5Url": "https://m.mcd.cn/mcp/scanToPay?orderId=X3",
                      "orderDetail": {"expirePayTime": "2026-10-12 18:45:10"}}
    card = draft.payment_card()
    assert "扫码" in card or "支付链接" in card
    assert "https://m.mcd.cn/mcp/scanToPay?orderId=X3" in card
    assert "18:45:10" in card
    assert "X3" in card
    # dry-run 未提交时不应产出支付卡片
    empty = build_order_draft(FakeClient(), _pickup_context(), _plan())
    assert empty.payment_card() == ""


def test_describe_order_renders_human_readable():
    text = describe_order({
        "orderId": "X4", "orderStatus": "待支付", "status": "10",
        "storeName": "某门店", "storeAddress": "某地址", "takeWay": "堂食",
        "createTime": "2026-10-12 18:30:10", "expirePayTime": "2026-10-12 18:45:10",
        "totalAmount": "40", "orderProductList": [
            {"productName": "牛肉堡单品精选", "quantity": 2, "price": "14",
             "comboItemList": [{"itemName": "汉堡包", "itemQuantity": 1}]},
        ],
    })
    assert "待支付" in text
    assert "某门店" in text
    assert "牛肉堡单品精选 ×2" in text
    assert "汉堡包 ×1" in text, "套餐子项应展开"
    assert "不会产生扣款" in text


def test_describe_order_marks_cancelled():
    text = describe_order({"orderId": "X5", "orderStatus": "订单已取消", "status": "60"})
    assert "已取消" in text
    assert "不产生任何消费" in text


# ------------------------------------------------------- 订单状态语义与订单列表
@pytest.mark.parametrize("text,group", [
    ("待支付", "pending_payment"),
    ("未支付", "pending_payment"),
    ("配餐中-已支付", "paid"),
    ("配送中", "paid"),
    ("已完成", "done"),
    ("已评价", "done"),
    ("订单已取消", "cancelled"),
    ("已关闭", "cancelled"),
])
def test_status_group_mapping(text, group):
    assert status_group(text) == group


def test_status_group_unknown_returns_empty():
    """无法识别的状态必须返回空串,让调用方选择「不动它」,而不是猜。"""
    assert status_group("某种新状态") == ""
    assert status_group("") == ""


def test_status_codes_match_official_schema():
    """status 码取自 query-order 的 outputSchema 说明。"""
    assert ORDER_STATUS_CODES["1"] == "待支付"
    assert ORDER_STATUS_CODES["6"] == "已完成"
    assert ORDER_STATUS_CODES["7"] == "已取消"


def test_is_pending_payment():
    assert is_pending_payment({"orderStatus": "待支付"}) is True
    assert is_pending_payment({"orderStatus": "订单已完成"}) is False
    assert is_pending_payment({"orderStatus": "订单已取消"}) is False
    assert is_pending_payment({}) is False
    assert is_pending_payment(None) is False


class FakeOrderListClient:
    """带历史订单列表的假客户端。"""

    def __init__(self, orders, query_sequence=None):
        self.orders = orders
        self.query_sequence = list(query_sequence or [])
        self.cancel_calls = []
        self.query_calls = []

    def order_list(self):
        return list(self.orders)

    def query_order(self, order_id):
        self.query_calls.append(order_id)
        if self.query_sequence:
            return self.query_sequence.pop(0)
        return {"orderId": order_id, "orderStatus": "待支付"}

    def cancel_order(self, order_id, cancel_reason_code=-1):
        self.cancel_calls.append({"orderId": order_id, "reason": cancel_reason_code})
        return {"cancelResult": True, "orderId": order_id}


def test_pending_payment_orders_filters_and_sorts():
    client = FakeOrderListClient([
        {"orderId": "A", "orderStatus": "订单已完成", "createTime": "2026-01-01 10:00:00"},
        {"orderId": "B", "orderStatus": "待支付", "createTime": "2026-02-01 10:00:00"},
        {"orderId": "C", "orderStatus": "订单已取消", "createTime": "2026-03-01 10:00:00"},
        {"orderId": "D", "orderStatus": "待支付", "createTime": "2026-04-01 10:00:00"},
    ])
    pending = pending_payment_orders(client)
    assert [o["orderId"] for o in pending] == ["D", "B"], "只留待支付,并按创建时间倒序"


def test_pending_payment_orders_empty():
    client = FakeOrderListClient([{"orderId": "A", "orderStatus": "订单已完成"}])
    assert pending_payment_orders(client) == []


def test_summarise_order_is_one_line():
    text = summarise_order({
        "orderStatus": "待支付", "realTotalAmount": "40",
        "orderProductList": [{"productName": "巨无霸", "quantity": 2},
                             {"productName": "中薯条", "quantity": 1}],
    })
    assert "待支付" in text and "40" in text
    assert "巨无霸×2" in text
    assert "\n" not in text


# --------------------------------------------------------------------- 轮询
def test_wait_returns_immediately_on_terminal_state():
    client = FakeOrderListClient([], query_sequence=[
        {"orderId": "X", "orderStatus": "已完成"},
    ])
    slept = []
    result = wait_for_payment(client, "X", timeout=60, interval=5, sleep=slept.append)
    assert result["group"] == "done"
    assert result["paid"] is True
    assert result["polls"] == 1
    assert slept == [], "已到终态就不该再睡"


def test_wait_polls_until_paid():
    client = FakeOrderListClient([], query_sequence=[
        {"orderId": "X", "orderStatus": "待支付"},
        {"orderId": "X", "orderStatus": "待支付"},
        {"orderId": "X", "orderStatus": "配餐中-已支付"},
    ])
    slept = []
    result = wait_for_payment(client, "X", timeout=600, interval=5, sleep=slept.append)
    assert result["paid"] is True
    assert result["group"] == "paid"
    assert result["polls"] == 3
    assert len(slept) == 2, "每次未到终态才睡一次"


def test_wait_times_out_gracefully(monkeypatch):
    """超时要如实返回 timedOut,而不是抛异常或谎报成功。

    用一个假的 ``time.monotonic`` 让「已经过了很久」成立,避免真的等待。
    """
    import time as real_time
    import mcdteam.order as order_module  # noqa: F401  确保模块已加载

    ticks = {"n": 0.0}

    def fake_monotonic():
        ticks["n"] += 100.0     # 每问一次时间就推进 100 秒
        return ticks["n"]

    monkeypatch.setattr(real_time, "monotonic", fake_monotonic)
    client = FakeOrderListClient([], query_sequence=[
        {"orderId": "X", "orderStatus": "待支付"},
    ] * 50)
    result = wait_for_payment(client, "X", timeout=150, interval=1,
                              sleep=lambda _s: None)
    assert result["timedOut"] is True
    assert result["paid"] is False
    assert result["orderStatus"] == "待支付"


def test_wait_reports_query_error_without_crashing():
    class BrokenClient:
        def query_order(self, order_id):
            raise MCPBusinessError("订单号不存在!", tool="query-order")

    result = wait_for_payment(BrokenClient(), "X", timeout=10, interval=1,
                              sleep=lambda _s: None)
    assert result["error"] == "订单号不存在!"
    assert result["paid"] is False


def test_wait_cancelled_order():
    client = FakeOrderListClient([], query_sequence=[
        {"orderId": "X", "orderStatus": "订单已取消"},
    ])
    result = wait_for_payment(client, "X", timeout=60, interval=5, sleep=lambda _s: None)
    assert result["cancelled"] is True
    assert result["paid"] is False
