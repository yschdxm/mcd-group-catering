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
