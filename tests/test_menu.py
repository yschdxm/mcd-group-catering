"""菜单角色分类与时段/份量推断测试。

角色分类是团餐配额能否成立的前提,因此对容易误判的品类(炸鸡、
瓶装水、蘸酱、套餐)都有断言。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mcdteam.data.menu import (  # noqa: E402
    MenuItem,
    Role,
    build_catalog,
    classify,
    portion_factor,
    spice_level,
)


@pytest.mark.parametrize("name,expected", [
    ("巨无霸", Role.MAIN),
    ("麦辣鸡腿汉堡", Role.MAIN),
    ("板烧鸡腿堡", Role.MAIN),
    ("酥酥多笋卷", Role.MAIN),
    ("薄皮焦香V翅", Role.SIDE),
    ("麦乐鸡5块", Role.SIDE),
    ("大薯条", Role.SIDE),
    ("蘸酱麦麦脆汁鸡", Role.SIDE),
    ("中杯可乐", Role.DRINK),
    ("小杯鲜萃咖啡", Role.DRINK),
    ("纯悦", Role.DRINK),
    ("圆筒冰淇淋", Role.DESSERT),
    ("奥利奥麦旋风", Role.DESSERT),
    ("猪柳麦满分", Role.BREAKFAST),
    ("小食拼盘多人餐", Role.COMBO),
])
def test_classify(name, expected):
    assert classify(name) is expected


def test_classify_uses_category_and_tags():
    # 名称看不出品类时,分类名能兜底
    assert classify("神秘新品", category="小食拼盘/多人餐") is Role.COMBO


@pytest.mark.parametrize("name,level", [
    ("麦辣鸡腿汉堡", 1),
    ("香辣鸡腿堡", 2),
    ("板烧鸡腿堡", 0),
    ("原味板烧鸡腿麦满分", 0),
    ("巨无霸", 0),
])
def test_spice_level(name, level):
    assert spice_level(name) == level


def test_portion_factor_reads_size_hints():
    assert portion_factor("迷你薯条") < 1.0
    assert portion_factor("大杯可乐") > 1.0
    assert portion_factor("巨无霸") == 1.0


def _payload():
    return {"categories": [
        {"name": "人气热卖", "meals": [{"code": "1100", "tags": ["爆款"]},
                                    {"code": "1440"}]},
        {"name": "小食", "meals": [{"code": "9001", "tags": ["小食拼盘"]}]},
    ]}


def test_build_catalog_marks_sold_out_when_price_missing():
    catalog = build_catalog(_payload(), {"1100": {"productName": "巨无霸", "subtotal": 2550}})
    assert len(catalog) == 3
    assert catalog.get("1100").price == 2550
    assert catalog.get("1440").sold_out is True
    assert catalog.priced()[0].code == "1100"


def test_build_catalog_dedupes_same_name():
    payload = {"categories": [{"name": "套餐", "meals": [
        {"code": "A1"}, {"code": "A2"}]}]}
    prices = {
        "A1": {"productName": "汉堡开心乐园餐", "subtotal": 2350},
        "A2": {"productName": "汉堡开心乐园餐", "subtotal": 2300},
    }
    catalog = build_catalog(payload, prices)
    assert len(catalog) == 1
    item = catalog.get("A2")
    assert item is not None and item.price == 2300  # 取更低的价格
    assert "A1" in item.aliases


def test_menu_item_flags():
    beef = MenuItem(code="1100", name="巨无霸", price=2550, role=Role.MAIN)
    assert beef.contains_beef and not beef.contains_chicken
    assert not beef.is_vegetarian
    water = MenuItem(code="900", name="纯悦", price=750, role=Role.DRINK)
    assert water.is_vegetarian
    condiment = MenuItem(code="800", name="日式鲜磨山葵酱", price=300, role=Role.SIDE)
    assert condiment.is_condiment


def test_protein_per_yuan_guards_zero_price():
    item = MenuItem(code="1", name="测试", price=0, role=Role.MAIN)
    assert item.protein_per_yuan() == 0.0
