"""营养文本表格解析器测试。

重点覆盖三个真实存在的边界:`nutritionDescription` 为 ``null``、
说明列里带逗号、以及重复行。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mcdteam.data.nutrition import (  # noqa: E402
    NutritionBook,
    normalize_name,
    parse_nutrition_table,
)

SAMPLE = """[4]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
  中薯条,null,1210,289,4,12,38,165,18
  芝士安格斯厚牛堡,能量约鲈鱼1条,2914,696,32,44,43,897,150
  小杯玉米杯,null,223,53,2,1,7,1,6
"""

BOMBED = "\ufeff" + SAMPLE


def test_parse_basic_table():
    rows = parse_nutrition_table(SAMPLE)
    assert len(rows) == 4
    first = rows[0]
    assert first.product_name == "猪柳麦满分"
    assert first.energy_kcal == 308
    assert first.protein == 16
    assert first.description is None


def test_parse_handles_bom():
    """该接口返回的文本在落盘后可能带 BOM,必须能正常解析。"""
    rows = parse_nutrition_table(BOMBED)
    assert len(rows) == 4
    assert rows[0].product_name == "猪柳麦满分"


def test_parse_keeps_description_with_commas():
    rows = parse_nutrition_table(SAMPLE)
    entry = [r for r in rows if r.product_name.startswith("芝士安格斯")][0]
    assert entry.description == "能量约鲈鱼1条"
    # 数值列不能被说明里的逗号挤错位
    assert entry.energy_kcal == 696
    assert entry.protein == 32
    assert entry.calcium == 150


def test_parse_rejects_empty():
    with pytest.raises(ValueError):
        parse_nutrition_table("")
    with pytest.raises(ValueError):
        parse_nutrition_table("   \n  ")


def test_parse_rejects_missing_header():
    with pytest.raises(ValueError):
        parse_nutrition_table("猪柳麦满分,null,1288,308,16,16,24,781,213")


def test_normalize_name_strips_decoration():
    assert normalize_name("【美汁源】“黄金橙橙”") == normalize_name("美汁源黄金橙橙")
    assert normalize_name(" 中薯条 ") == "中薯条"
    assert normalize_name("大杯怡泉+C") == "大杯怡泉c"


def test_book_exact_and_fuzzy_match():
    book = NutritionBook.from_raw(SAMPLE)
    assert book.get("中薯条") is not None
    # 精确优先
    assert book.get("猪柳麦满分").energy_kcal == 308
    # 带后缀的写法走模糊匹配
    matched = book.match("中薯条(大)")
    assert matched is not None and matched.product_name == "中薯条"
    # 不应把「麦香鸡」错配成别的
    assert book.match("完全不存在的餐品") is None


def test_book_deduplicates_repeated_rows():
    duplicated = SAMPLE + "  小杯玉米杯,null,223,53,2,1,7,1,6\n"
    book = NutritionBook.from_raw(duplicated)
    assert len(book) == 5


def test_real_fixture_parses():
    """仓库内快照必须可解析,否则离线演示会失效。"""
    fixture = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fixtures", "nutrition_raw.txt")
    if not os.path.exists(fixture):
        pytest.skip("fixture 不存在")
    with open(fixture, "r", encoding="utf-8-sig") as handle:
        book = NutritionBook.from_raw(handle.read())
    assert len(book) >= 150
    assert book.get("巨无霸").energy_kcal == 513
