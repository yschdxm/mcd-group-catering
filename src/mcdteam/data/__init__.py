"""数据层:菜单、营养、优惠券三类原料的解析与索引。"""

from .coupons import Coupon, Promotion, parse_coupon, parse_coupons, parse_promotions
from .menu import (
    MenuCatalog,
    MenuItem,
    Role,
    build_catalog,
    classify,
    collect_price_map,
    portion_factor,
    spice_level,
)
from .nutrition import Nutrition, NutritionBook, normalize_name, parse_nutrition_table

__all__ = [
    "Coupon",
    "Promotion",
    "parse_coupon",
    "parse_coupons",
    "parse_promotions",
    "MenuCatalog",
    "MenuItem",
    "Role",
    "build_catalog",
    "classify",
    "collect_price_map",
    "portion_factor",
    "spice_level",
    "Nutrition",
    "NutritionBook",
    "normalize_name",
    "parse_nutrition_table",
]
