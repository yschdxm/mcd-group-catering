"""多门店比价测试。

全部使用假的 session/client,不发任何网络请求。
重点验证:排序正确、单店失败不影响整体、节省金额计算、渲染可用。
"""

from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from mcdteam.compare import (  # noqa: E402
    StoreComparison,
    StorePrice,
    candidate_stores,
    compare_stores,
    render_comparison,
    render_comparison_markdown,
)
from mcdteam.data.menu import MenuCatalog, MenuItem, Role  # noqa: E402
from mcdteam.mcp.context import FulfillmentMode, StoreContext  # noqa: E402
from mcdteam.mcp.errors import MCPBusinessError  # noqa: E402
from mcdteam.solve.constraints import CateringRequirements  # noqa: E402
from mcdteam.solve.planner import CateringPlanner  # noqa: E402


def _plan(price_cents: int, store_code: str = "S1"):
    """造一个固定金额的方案:单价可控,便于断言排序。"""
    catalog = MenuCatalog([
        MenuItem(code="M1", name=f"测试汉堡{store_code}", price=price_cents,
                 role=Role.MAIN),
    ], store_code=store_code, store_name=f"门店{store_code}")
    requirements = CateringRequirements(headcount=2, budget_total=500, effort=4)
    plan = CateringPlanner(catalog, requirements).solve()
    plan.store_code = store_code
    plan.store_name = f"门店{store_code}"
    return plan


class FakeClient:
    def __init__(self, stores):
        self.stores = stores
        self.delivery_calls = []

    def resolve_delivery_stores(self, address_id, be_type=6):
        self.delivery_calls.append({"addressId": address_id, "beType": be_type})
        return list(self.stores)

    def ensure_ok(self, tool, arguments=None, **kwargs):
        class _Result:
            @staticmethod
            def require_data():
                return {"addresses": [{"addressId": "ADDR1"}]}
        return _Result()


class FakeSession:
    """按门店编码返回不同金额的方案;可指定某门店抛错。"""

    def __init__(self, prices: dict, failing=(), stores=None,
                 catalog_empty=()):
        self.prices = prices
        self.failing = set(failing)
        self.catalog_empty = set(catalog_empty)
        self.config = type("C", (), {"address_id": "ADDR1", "offline": False})()
        self.client = FakeClient(stores if stores is not None else [
            {"storeCode": code, "storeName": f"门店{code}", "beCode": f"BE{code}",
             "businessStatus": True}
            for code in prices
        ])
        self.loaded = []

    def _with_gm_service(self, context):
        return context

    def load_catalog_for(self, context, max_items=0):
        self.loaded.append(context.store_code)
        if context.store_code in self.catalog_empty:
            return MenuCatalog([], store_code=context.store_code)
        catalog = MenuCatalog([
            MenuItem(code=f"M-{context.store_code}", name="测试餐品",
                     price=1000, role=Role.MAIN),
        ], store_code=context.store_code, store_name=context.store_name)
        return catalog

    def plan_for(self, context, catalog, requirements, verify=True):
        if context.store_code in self.failing:
            raise MCPBusinessError("门店暂不营业", tool="query-meals")
        plan = _plan(self.prices[context.store_code], context.store_code)
        plan.store_name = context.store_name
        return plan


def _requirements(headcount=8, budget=300):
    req = CateringRequirements(headcount=headcount, budget_total=budget, effort=8)
    req.mode = FulfillmentMode.DELIVERY
    return req


# --------------------------------------------------------------- 候选门店获取
def test_candidate_stores_passes_be_type():
    client = FakeClient([{"storeCode": "S1"}])
    candidate_stores(client, "ADDR1", be_type=6)
    assert client.delivery_calls[0]["beType"] == 6


# --------------------------------------------------------------------- 排序
def test_compare_ranks_by_payable_amount():
    session = FakeSession({"S1": 30000, "S2": 28000, "S3": 29500})
    comparison = compare_stores(session, _requirements())
    assert [c.store_code for c in comparison.ranked] == ["S2", "S3", "S1"]
    assert comparison.best.store_code == "S2"
    assert comparison.best.payable_yuan == pytest.approx(280.0)
    assert comparison.worst.store_code == "S1"
    assert comparison.saving_cents == 2000
    assert comparison.saving_cents / 100 == pytest.approx(20.0)


def test_compare_reports_equal_prices():
    session = FakeSession({"S1": 30000, "S2": 30000})
    comparison = compare_stores(session, _requirements())
    assert comparison.saving_cents == 0
    assert any("相同" in note for note in comparison.notes)


def test_compare_handles_single_store():
    session = FakeSession({"S1": 30000})
    comparison = compare_stores(session, _requirements())
    assert comparison.best.store_code == "S1"
    assert comparison.saving_cents == 0


# ------------------------------------------------------------- 单店失败容错
def test_one_failing_store_does_not_break_comparison():
    session = FakeSession({"S1": 30000, "S2": 28000}, failing={"S2"})
    comparison = compare_stores(session, _requirements())
    assert comparison.best.store_code == "S1"
    failed = [c for c in comparison.ranked if not c.ok]
    assert len(failed) == 1 and failed[0].store_code == "S2"
    assert "暂不营业" in failed[0].error
    assert failed[0].payable_yuan == 0


def test_closed_store_is_reported_without_calling():
    session = FakeSession({"S1": 30000})
    session.client.stores.append(
        {"storeCode": "S9", "storeName": "歇业门店", "beCode": "BE9",
         "businessStatus": False})
    comparison = compare_stores(session, _requirements())
    closed = [c for c in comparison.candidates if c.store_code == "S9"][0]
    assert closed.ok is False
    assert "不营业" in closed.error
    assert "S9" not in session.loaded, "歇业门店不该再去拉菜单"


def test_store_without_be_code_for_delivery_is_skipped():
    session = FakeSession({"S1": 30000})
    session.client.stores.append(
        {"storeCode": "S8", "storeName": "无码门店", "beCode": "",
         "businessStatus": True})
    comparison = compare_stores(session, _requirements())
    entry = [c for c in comparison.candidates if c.store_code == "S8"][0]
    assert entry.ok is False
    assert "beCode" in entry.error


def test_all_stores_failing_gives_actionable_note():
    session = FakeSession({"S1": 30000, "S2": 28000}, failing={"S1", "S2"})
    comparison = compare_stores(session, _requirements())
    assert comparison.best is None
    assert any("没能生成方案" in note for note in comparison.notes)
    assert comparison.saving_cents == 0


def test_empty_catalog_is_treated_as_failure():
    session = FakeSession({"S1": 30000}, catalog_empty={"S1"})
    comparison = compare_stores(session, _requirements())
    assert comparison.best is None
    entry = comparison.candidates[0]
    assert entry.ok is False
    assert "没有可售餐品" in entry.error


# ------------------------------------------------------------- 参数与边界
def test_max_stores_limits_and_notes():
    session = FakeSession({f"S{i}": 30000 + i * 100 for i in range(5)})
    comparison = compare_stores(session, _requirements(), max_stores=2)
    assert len(comparison.candidates) == 2
    assert any("只比价前 2 家" in note for note in comparison.notes)


def test_pickup_mode_is_rejected():
    session = FakeSession({"S1": 30000})
    requirements = _requirements()
    requirements.mode = FulfillmentMode.PICKUP
    with pytest.raises(ValueError):
        compare_stores(session, requirements)


def test_missing_address_raises():
    """账号里一个地址都没有时必须明确报错,而不是静默给出空比价。"""
    from mcdteam.mcp.errors import MCPError

    session = FakeSession({"S1": 30000})
    session.config.address_id = ""

    class NoAddressResult:
        @staticmethod
        def require_data():
            return {"addresses": []}

    session.client.ensure_ok = lambda *a, **k: NoAddressResult()
    with pytest.raises(MCPError) as excinfo:
        compare_stores(session, _requirements())
    assert "配送地址" in str(excinfo.value)


def test_no_candidate_stores_raises():
    session = FakeSession({})
    session.client.stores = []
    from mcdteam.mcp.errors import MCPError

    with pytest.raises(MCPError):
        compare_stores(session, _requirements())


def test_group_mode_uses_be_type_6():
    session = FakeSession({"S1": 30000})
    requirements = _requirements()
    requirements.mode = FulfillmentMode.GROUP
    compare_stores(session, requirements, max_stores=1)
    assert session.client.delivery_calls[0]["beType"] == 6


def test_compare_does_not_mutate_caller_requirements():
    session = FakeSession({"S1": 30000, "S2": 31000})
    requirements = _requirements()
    original_effort = requirements.effort
    original_notes = list(requirements.notes)
    compare_stores(session, requirements, effort=99)
    assert requirements.effort == original_effort, "不能改调用方的搜索强度"
    assert requirements.notes == original_notes
    assert requirements.store_code == ""


# --------------------------------------------------------------------- 渲染
def test_render_comparison_contains_ranking_and_saving():
    session = FakeSession({"S1": 30000, "S2": 28000})
    comparison = compare_stores(session, _requirements())
    text = render_comparison(comparison, color=False)
    assert "多门店比价" in text
    assert "门店S2" in text
    assert "280.00" in text
    assert "省" in text and "20.00" in text
    assert "calculate-price" in text


def test_render_comparison_marks_failed_store():
    session = FakeSession({"S1": 30000}, failing=set())
    session.client.stores.append({"storeCode": "S7", "storeName": "坏门店",
                                  "beCode": "BE7", "businessStatus": False})
    comparison = compare_stores(session, _requirements())
    text = render_comparison(comparison, color=False)
    assert "不营业" in text


def test_render_comparison_markdown_table():
    session = FakeSession({"S1": 30000, "S2": 28000})
    comparison = compare_stores(session, _requirements())
    md = render_comparison_markdown(comparison)
    assert md.startswith("# 多门店比价")
    assert "| 排名 |" in md
    assert "门店S2" in md
    assert "¥280.00" in md


def test_comparison_to_dict_serialisable():
    import json
    session = FakeSession({"S1": 30000, "S2": 28000})
    comparison = compare_stores(session, _requirements())
    payload = json.loads(json.dumps(comparison.to_dict(), ensure_ascii=False))
    assert payload["best"]["storeCode"] == "S2"
    assert payload["savingYuan"] == 20.0
    assert len(payload["candidates"]) == 2


def test_store_price_to_dict_shape():
    entry = StorePrice(store_code="S1", store_name="门店1")
    payload = entry.to_dict()
    assert payload["ok"] is False
    assert payload["payableYuan"] is None
    assert payload["storeName"] == "门店1"
