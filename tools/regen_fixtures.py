"""重新生成离线快照。

用途:把仓库内置的演示快照替换成**你自己门店**的真实数据,
这样 `--offline` 演示的就是你所在城市的菜单与价格。

用法::

    export MCD_MCP_TOKEN="..."
    python tools/regen_fixtures.py --store 1950564 --out fixtures
    # 团餐场景(需要 beCode,可由门店接口自动解析)
    python tools/regen_fixtures.py --store 1950564 --mode group --city 北京

生成的快照与 MCP 客户端读取的命名规则一致::

    fixtures/query-meals.<storeCode>.json
    fixtures/price-map.store<storeCode>.json
    fixtures/query-store-coupons.<storeCode>.json
    fixtures/nutrition_raw.txt

注意:快照只包含门店编码、餐品编码、菜名与价格,不含任何个人信息。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mcdteam.config import AppConfig  # noqa: E402
from mcdteam.data.menu import collect_price_map  # noqa: E402
from mcdteam.mcp.client import MCPClient  # noqa: E402
from mcdteam.mcp.context import FulfillmentMode, StoreContext  # noqa: E402
from mcdteam.mcp.errors import MCPError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="重新生成离线快照")
    parser.add_argument("--store", required=True, help="门店 storeCode")
    parser.add_argument("--out", default="fixtures", help="快照输出目录")
    parser.add_argument("--mode", default="pickup",
                        help="pickup / drive_thru / delivery / group")
    parser.add_argument("--city", default="", help="团餐/外送场景用于解析 beCode 的城市")
    parser.add_argument("--keyword", default="", help="门店关键词")
    args = parser.parse_args()

    config = AppConfig.load(store_code=args.store, city=args.city)
    if not config.has_token:
        print("✘ 需要设置 MCD_MCP_TOKEN 环境变量", file=sys.stderr)
        return 2

    os.makedirs(args.out, exist_ok=True)
    client = MCPClient(config.token, snapshot_dir=args.out, allow_snapshot=False)
    mode = FulfillmentMode.parse(args.mode)

    be_code = ""
    if mode.requires_be_code:
        if mode is FulfillmentMode.GROUP or mode is FulfillmentMode.DELIVERY:
            addresses = client.ensure_ok("delivery-query-addresses", {}).require_data() or {}
            items = addresses.get("addresses", []) if isinstance(addresses, dict) else []
            if not items:
                print("✘ 账号没有配送地址,无法解析外送/团餐门店", file=sys.stderr)
                return 3
            records = client.resolve_delivery_stores(
                str(items[0]["addressId"]), be_type=(6 if mode is FulfillmentMode.GROUP else 2))
        else:
            records = client.resolve_store_code(args.city, args.keyword, be_type=5)
        for record in records:
            if str(record.get("storeCode")) == str(args.store) and record.get("beCode"):
                be_code = str(record["beCode"])
                break
        if not be_code and records:
            be_code = str(records[0].get("beCode") or "")
        if not be_code:
            print(f"✘ 未能解析 {mode.label} 场景的 beCode", file=sys.stderr)
            return 3

    context = StoreContext(store_code=args.store, mode=mode, be_code=be_code, city=args.city)

    print(f"→ 拉取门店菜单 {context.describe()}")
    payload = client.query_meals(
        context.store_code, order_type=context.order_type, be_type=context.be_type,
        be_code=context.be_code)
    codes = [str(meal.get("code", "")) for group in payload.get("categories", []) or []
             for meal in group.get("meals", []) or []]
    codes = [c for c in dict.fromkeys(codes) if c]
    print(f"  可售编码 {len(codes)} 个")

    def progress(message: str) -> None:
        print(f"  … {message}")

    price_map = collect_price_map(client, context, codes, progress=progress)
    print(f"  取价成功 {len(price_map)} 个")

    meals_path = os.path.join(args.out, f"query-meals.{args.store}.json")
    with open(meals_path, "w", encoding="utf-8") as handle:
        json.dump({"success": True, "code": 200, "message": "请求成功",
                   "data": payload}, handle, ensure_ascii=False, indent=2)
    print(f"  ✔ {meals_path}")

    price_path = os.path.join(args.out, f"price-map.store{args.store}.json")
    snapshot = {
        "storeCode": args.store,
        "orderType": context.order_type,
        "beType": context.be_type,
        "capturedAt": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "products": [
            {"productCode": code, "productName": info.get("productName", ""),
             "price": int(info.get("subtotal") or 0)}
            for code, info in sorted(price_map.items())
        ],
    }
    with open(price_path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
    print(f"  ✔ {price_path}")

    try:
        coupons = client.store_coupons(
            context.store_code, order_type=context.order_type, be_type=context.be_type,
            be_code=context.be_code)
        coupon_path = os.path.join(args.out, f"query-store-coupons.{args.store}.json")
        with open(coupon_path, "w", encoding="utf-8") as handle:
            json.dump({"success": True, "code": 200, "message": "请求成功",
                       "data": coupons}, handle, ensure_ascii=False, indent=2)
        print(f"  ✔ {coupon_path}")
    except MCPError as exc:
        print(f"  ⚠ 门店券快照跳过:{exc}")

    try:
        raw = client.nutrition_raw()
        if raw.strip():
            nutrition_path = os.path.join(args.out, "nutrition_raw.txt")
            with open(nutrition_path, "w", encoding="utf-8") as handle:
                handle.write(raw)
            print(f"  ✔ {nutrition_path}")
        else:
            print("  ⚠ 营养接口返回空,沿用原有快照")
    except MCPError as exc:
        print(f"  ⚠ 营养快照跳过:{exc}")

    print("完成。可以用 --offline 复现同样的菜单了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
