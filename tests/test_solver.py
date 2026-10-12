"""自然语言需求解析、配额推算与求解器测试。

这部分是项目的核心逻辑,断言集中在三件事上:

1. 口语能正确变成结构化约束(人数/预算/时间/忌口/偏好);
2. 求解结果绝不低于预算、绝不踩硬性忌口;
3. 主食配额必须被满足(团餐最常见的翻车点)。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mcdteam.data.coupons import parse_coupon, parse_coupons, parse_promotions  # noqa: E402
from mcdteam.data.menu import MenuItem, MenuCatalog, Role  # noqa: E402
from mcdteam.data.nutrition import Nutrition  # noqa: E402
from mcdteam.mcp.context import FulfillmentMode, StoreContext  # noqa: E402
from mcdteam.solve.constraints import (  # noqa: E402
    CateringRequirements,
    parse_requirements,
    role_quota,
)
from mcdteam.solve.planner import (  # noqa: E402
    CateringPlanner,
    build_candidate_pool,
)


# ------------------------------------------------------------------ 需求解析
def test_parse_headcount_and_budget():
    req = parse_requirements("周五中午14个人,预算480元")
    assert req.headcount == 14
    assert req.budget_total == 480
    assert req.budget_cents == 48000


def test_parse_chinese_numerals():
    req = parse_requirements("十二个人吃饭")
    assert req.headcount == 12
    req = parse_requirements("二十三个人")
    assert req.headcount == 23


def test_parse_per_person_budget_is_not_total():
    req = parse_requirements("10个人,人均35元")
    assert req.budget_per_person == 35
    assert req.budget_total is None
    assert req.budget_yuan == 350


def test_parse_time_and_meal_period():
    req = parse_requirements("明天12点送到", now=datetime(2026, 10, 12, 9, 0))
    assert req.meal_time == "lunch"
    assert req.reserve_at is not None
    assert req.reserve_at.strftime("%Y-%m-%d %H:%M") == "2026-10-13 12:00"


def test_parse_past_time_rolls_to_next_day():
    req = parse_requirements("11点送到", now=datetime(2026, 10, 12, 15, 0))
    assert req.reserve_at is not None
    assert req.reserve_at.day == 13


def test_parse_avoid_rules():
    req = parse_requirements("8个人 不吃辣 有人不吃牛肉 一个乳糖不耐 两位素食")
    assert req.avoid_spicy and req.avoid_beef and req.avoid_dairy and req.vegetarian
    assert req.has_hard_dietary
    assert "不吃辣" in req.hard_rule_summary()


def test_parse_preferences_and_targets():
    req = parse_requirements("6个人 要低卡 高蛋白 人均不超过600大卡 人均至少30克蛋白质")
    assert "low_calorie" in req.preferences
    assert "high_protein" in req.preferences
    assert req.max_kcal_per_person == 600
    assert req.min_protein_per_person == 30


def test_parse_total_calorie_becomes_per_person():
    req = parse_requirements("10个人 总共6000大卡")
    assert req.max_kcal_per_person == 600


def test_parse_kids_and_mode():
    req = parse_requirements("20个人 其中3个小朋友 送到公司")
    assert req.kids == 3
    assert req.mode is FulfillmentMode.DELIVERY


def test_role_quota_main_always_covered():
    req = CateringRequirements(headcount=14)
    quota = role_quota(req)
    assert quota["main"] == 14          # 每人一份主食
    assert quota["side"] >= 1
    assert quota["drink"] >= 1           # 人数 >= 6 默认配饮料


def test_role_quota_children_reduce_mains():
    adults = role_quota(CateringRequirements(headcount=10))
    with_kids = role_quota(CateringRequirements(headcount=10, kids=5))
    assert with_kids["main"] < adults["main"]


# --------------------------------------------------------------------- 优惠券
def test_parse_threshold_coupon():
    coupon = parse_coupon({
        "title": "满50元减12元", "couponId": "X", "couponCode": "Y",
        "tradeDateTime": "2026-10-12 10:30:00-2026-10-16 23:59:59",
    })
    assert coupon.kind == "threshold"
    assert coupon.threshold_yuan == 50
    assert coupon.discount_yuan == 12
    assert coupon.valid_to is not None and coupon.valid_to.day == 16


def test_parse_discount_coupon():
    coupon = parse_coupon({"title": "全场8.5折", "couponId": "A", "couponCode": "B"})
    assert coupon.kind == "discount"
    assert coupon.discount_rate == pytest.approx(0.85)


def test_parse_bogo_and_dedupe():
    records = [
        {"couponName": "中薯条买一送一", "id": "1", "code": "C1"},
        {"couponName": "中薯条买一送一", "id": "1", "code": "C1"},
    ]
    coupons = parse_coupons(records)
    assert len(coupons) == 1
    assert coupons[0].kind == "bogo"
    assert coupons[0].buy == 1 and coupons[0].free == 1


def test_parse_promotions_splits_include_exclude():
    promotions = parse_promotions([{
        "promotionId": "P1", "promotionType": 31, "ruleCategory": 40,
        "startTime": "2026-10-01 00:00:00", "endTime": "2026-10-31 23:59:59",
        "products": [{"type": 1, "productCode": "1100"},
                     {"type": 2, "productCode": "1440"},
                     {"type": 3}],
        "ruleDetail": {"thresholdAmount": 20000, "discountAmount": 3000},
    }])
    assert len(promotions) == 1
    promotion = promotions[0]
    assert promotion.include_all is True
    assert "1100" in promotion.include_products
    assert "1440" in promotion.exclude_products
    assert promotion.threshold_yuan == 200
    assert promotion.discount_yuan == 30


# -------------------------------------------------------------------- 门店上下文
def test_store_context_requires_be_code_for_group():
    with pytest.raises(ValueError):
        StoreContext(store_code="1950564", mode=FulfillmentMode.GROUP)
    ok = StoreContext(store_code="1950564", mode=FulfillmentMode.GROUP, be_code="BE1")
    assert ok.order_type == 2 and ok.be_type == 6
    assert ok.store_arguments()["beCode"] == "BE1"


def test_store_context_pickup_omits_be_code():
    context = StoreContext(store_code="1950564", mode=FulfillmentMode.PICKUP)
    assert context.order_type == 1 and context.be_type == 1
    assert "beCode" not in context.store_arguments()


def test_fulfillment_mode_parse_aliases():
    assert FulfillmentMode.parse("团餐") is FulfillmentMode.GROUP
    assert FulfillmentMode.parse("麦乐送") is FulfillmentMode.DELIVERY
    assert FulfillmentMode.parse(5) is FulfillmentMode.DRIVE_THRU


# --------------------------------------------------------------------- 求解器
def _menu() -> MenuCatalog:
    def nut(kcal, protein):
        return Nutrition(product_name="x", energy_kcal=kcal, protein=protein)

    items = [
        MenuItem(code="1100", name="巨无霸", price=2550, role=Role.MAIN, nutrition=nut(513, 27)),
        MenuItem(code="1440", name="麦辣鸡腿汉堡", price=2250, role=Role.MAIN, nutrition=nut(485, 24)),
        MenuItem(code="1406", name="板烧鸡腿堡", price=2350, role=Role.MAIN, nutrition=nut(391, 23)),
        MenuItem(code="1300", name="麦香鱼", price=2250, role=Role.MAIN, nutrition=nut(325, 16)),
        MenuItem(code="1000", name="吉士汉堡包", price=1200, role=Role.MAIN, nutrition=nut(294, 16)),
        MenuItem(code="2000", name="中薯条", price=1350, role=Role.SIDE, nutrition=nut(289, 4)),
        MenuItem(code="2100", name="麦乐鸡5块", price=1400, role=Role.SIDE, nutrition=nut(213, 12)),
        MenuItem(code="3000", name="中杯可乐", price=1000, role=Role.DRINK, nutrition=nut(147, 0)),
        MenuItem(code="3100", name="纯牛奶", price=1050, role=Role.DRINK, nutrition=nut(129, 7)),
        MenuItem(code="4000", name="圆筒冰淇淋", price=500, role=Role.DESSERT, nutrition=nut(93, 2)),
        MenuItem(code="8000", name="日式鲜磨山葵酱", price=300, role=Role.SIDE),
    ]
    return MenuCatalog(items, store_code="0000001", store_name="测试门店")


def test_candidate_pool_excludes_condiments():
    pool = build_candidate_pool(_menu(), CateringRequirements(headcount=6))
    codes = {i.code for i in pool.all_items()}
    assert "8000" not in codes      # 蘸酱不参与配额
    assert "1100" in codes


def test_candidate_pool_respects_spicy_rule():
    pool = build_candidate_pool(_menu(), CateringRequirements(headcount=6, avoid_spicy=True))
    codes = {i.code for i in pool.all_items()}
    assert "1440" not in codes      # 麦辣鸡腿汉堡被排除


def test_candidate_pool_relaxes_when_mains_empty():
    """所有主食都辣时,必须放宽约束并如实记录,而不是返回空方案。"""
    catalog = MenuCatalog([
        MenuItem(code="1", name="香辣鸡腿堡", price=2000, role=Role.MAIN),
        MenuItem(code="2", name="中薯条", price=1000, role=Role.SIDE),
    ], store_code="1")
    pool = build_candidate_pool(catalog, CateringRequirements(headcount=4, avoid_spicy=True))
    assert pool.pick(Role.MAIN), "放宽后应至少有一个可用主食"
    assert pool.relaxations and "不吃辣" in pool.relaxations[0]


def test_planner_respects_budget_and_main_quota():
    requirements = CateringRequirements(headcount=8, budget_total=200, effort=24)
    plan = CateringPlanner(_menu(), requirements).solve()
    assert plan.payable_cents <= requirements.budget_cents
    assert plan.role_counts().get("主食", 0) >= 8
    assert plan.lines


def test_planner_avoids_spicy_items():
    requirements = CateringRequirements(headcount=6, budget_total=200,
                                        avoid_spicy=True, effort=16)
    plan = CateringPlanner(_menu(), requirements).solve()
    assert all("辣" not in line.item.name for line in plan.lines)


def test_planner_is_deterministic_with_same_seed():
    requirements = CateringRequirements(headcount=6, budget_total=180, effort=12,
                                        random_seed=7)
    first = CateringPlanner(_menu(), requirements).solve()
    second = CateringPlanner(_menu(), requirements).solve()
    assert [(l.item.code, l.quantity) for l in first.lines] == \
           [(l.item.code, l.quantity) for l in second.lines]


def test_planner_reports_unspent_budget_honestly():
    requirements = CateringRequirements(headcount=2, budget_total=30, effort=12)
    plan = CateringPlanner(_menu(), requirements).solve()
    assert plan.payable_cents <= 3000
    assert plan.budget_delta_yuan >= 0


def test_planner_applies_best_threshold_coupon():
    from mcdteam.data.coupons import parse_coupon
    coupon = parse_coupon({
        "title": "满50元减10元", "couponId": "C", "couponCode": "K",
        "tradeDateTime": "2020-01-01 00:00:00-2099-01-01 00:00:00",
    })
    requirements = CateringRequirements(headcount=4, budget_total=120, effort=20)
    plan = CateringPlanner(_menu(), requirements, coupons=[coupon]).solve()
    if plan.subtotal_cents >= 5000:
        assert plan.discount_cents == 1000
        assert len(plan.coupons_applied) == 1


def test_plan_dict_serialisable():
    import json
    requirements = CateringRequirements(headcount=4, budget_total=100, effort=8)
    plan = CateringPlanner(_menu(), requirements).solve()
    payload = json.dumps(plan.to_dict(), ensure_ascii=False)
    assert "payableYuan" in payload


def test_report_renderers_do_not_crash():
    from mcdteam.report.render import render_chat_card, render_markdown, render_terminal
    requirements = parse_requirements("6个人 预算150 不吃辣")
    plan = CateringPlanner(_menu(), requirements, progress=None).solve()
    assert "麦麦团餐统筹官" in render_terminal(plan, requirements, color=False)
    assert "采购清单" in render_markdown(plan, requirements)
    assert "团餐已排好" in render_chat_card(plan, requirements)
