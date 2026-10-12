"""重新生成离线快照。

用途:把仓库内置的演示快照替换成**你自己门店**的真实数据,
这样 ``--offline`` 演示的就是你所在城市、你常用取餐方式的菜单与价格。

用法::

    export MCD_MCP_TOKEN="..."

    # 单个门店(到店自取)
    python tools/regen_fixtures.py --store 1950564

    # 外送场景:先按地址找门店,再抓这些门店
    python tools/regen_fixtures.py --mode delivery --address-id 1036613970144243390849093445

    # 团餐场景(自动解析 beCode 与 gmServiceCode)
    python tools/regen_fixtures.py --mode group --city 无锡 --store 1990684

生成的快照与 MCP 客户端的读取规则一致::

    fixtures/query-meals.<storeCode>.json
    fixtures/price-map.store<storeCode>.<orderType>.<beType>.json
    fixtures/query-store-coupons.<storeCode>.<orderType>.<beType>.json
    fixtures/nutrition_raw.txt

**价格与券的快照文件名带场景后缀**:同价门店的到店价与外送价不同,
不带后缀会互相覆盖。客户端优先读带场景后缀的文件。

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


def _write(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(f"  ✔ {path}")


def _resolve_delivery_records(client, mode: FulfillmentMode, address_id: str,
                              city: str, keyword: str, store: str) -> list:
    """按场景解析需要抓取的门店列表。"""
    if mode in (FulfillmentMode.DELIVERY, FulfillmentMode.GROUP):
        if not address_id:
            data = client.ensure_ok("delivery-query-addresses", {}).require_data() or {}
            addresses = data.get("addresses", []) if isinstance(data, dict) else []
            if not addresses:
                raise SystemExit("✘ 账号没有配送地址,无法解析外送/团餐门店")
            address_id = str(addresses[0]["addressId"])
            print(f"→ 使用默认配送地址:{addresses[0].get('fullAddress', '')[:40]}")
        be_type = 6 if mode is FulfillmentMode.GROUP else 2
        return client.resolve_delivery_stores(address_id, be_type=be_type)
    if mode is FulfillmentMode.DRIVE_THRU:
        return client.resolve_store_code(city, keyword, be_type=5)
    return client.resolve_store_code(city, keyword, be_type=1)


def capture_store(client: MCPClient, out_dir: str, mode: FulfillmentMode,
                  record: dict) -> bool:
    """抓取单个门店的快照,返回是否成功。"""
    store_code = str(record.get("storeCode", ""))
    store_name = str(record.get("storeName", "") or "")
    be_code = str(record.get("beCode", "") or "")
    context = StoreContext(store_code=store_code, store_name=store_name,
                           mode=mode, be_code=be_code)

    print(f"\n→ {store_name or store_code}({store_code})· {mode.label}")

    # 团餐需要助餐服务码才能取价
    if mode is FulfillmentMode.GROUP:
        try:
            services = client.meal_assistance(
                store_code, order_type=context.order_type, be_type=6, be_code=be_code)
            for service in services:
                if (service.get("enable") is not False
                        and service.get("gmServiceCode")):
                    context = context.with_gm_service(str(service["gmServiceCode"]))
                    if service.get("selected"):
                        break
            if context.gm_service_code:
                print(f"   助餐服务码 = {context.gm_service_code}")
        except MCPError as exc:
            print(f"   ⚠ 助餐服务查询失败:{exc.message}")

    try:
        payload = client.query_meals(
            context.store_code, order_type=context.order_type,
            be_type=context.be_type, be_code=context.be_code)
    except MCPError as exc:
        print(f"   ✘ 菜单查询失败:{exc.message}")
        return False

    codes = [str(meal.get("code", "")) for group in payload.get("categories", []) or []
             for meal in group.get("meals", []) or []]
    codes = [c for c in dict.fromkeys(codes) if c]
    print(f"   可售编码 {len(codes)} 个")
    if not codes:
        print("   ✘ 没有可售编码,跳过")
        return False

    def progress(message: str) -> None:
        print(f"   … {message}")

    price_map = collect_price_map(client, context, codes, progress=progress)
    print(f"   取价成功 {len(price_map)} 个")
    if not price_map:
        print("   ✘ 取价全部失败,跳过该门店")
        return False

    _write(os.path.join(out_dir, f"query-meals.{store_code}.json"),
           {"success": True, "code": 200, "message": "请求成功", "data": payload})

    suffix = f"{context.order_type}.{context.be_type}"
    _write(os.path.join(out_dir, f"price-map.store{store_code}.{suffix}.json"), {
        "storeCode": store_code,
        "orderType": context.order_type,
        "beType": context.be_type,
        "capturedAt": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "products": [
            {"productCode": code, "productName": info.get("productName", ""),
             "price": int(info.get("subtotal") or 0)}
            for code, info in sorted(price_map.items())
        ],
    })

    try:
        coupons = client.store_coupons(
            context.store_code, order_type=context.order_type,
            be_type=context.be_type, be_code=context.be_code)
        _write(os.path.join(out_dir, f"query-store-coupons.{store_code}.{suffix}.json"),
               {"success": True, "code": 200, "message": "请求成功", "data": coupons})
    except MCPError as exc:
        print(f"   ⚠ 门店券快照跳过:{exc.message}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="重新生成离线快照")
    parser.add_argument("--store", default="", help="门店 storeCode(可留空,按场景自动找)")
    parser.add_argument("--stores", default="",
                        help="多个门店编码,逗号分隔")
    parser.add_argument("--mode", default="pickup",
                        help="pickup / drive_thru / delivery / group")
    parser.add_argument("--out", default="fixtures", help="快照输出目录")
    parser.add_argument("--address-id", default="", help="配送地址 ID(外送/团餐)")
    parser.add_argument("--city", default="", help="用于搜索门店的城市")
    parser.add_argument("--keyword", default="", help="门店关键词")
    parser.add_argument("--max-stores", type=int, default=3,
                        help="外送/团餐场景最多抓几家,默认 3")
    parser.add_argument("--skip-nutrition", action="store_true",
                        help="不覆盖营养快照(营养表与门店无关)")
    args = parser.parse_args()

    config = AppConfig.load(store_code=args.store, city=args.city)
    if not config.has_token:
        print("✘ 需要设置 MCD_MCP_TOKEN 环境变量", file=sys.stderr)
        return 2

    os.makedirs(args.out, exist_ok=True)
    client = MCPClient(config.token, snapshot_dir=args.out, allow_snapshot=False)
    mode = FulfillmentMode.parse(args.mode)

    # 确定要抓的门店
    wanted = [s.strip() for s in args.stores.split(",") if s.strip()]
    if args.store:
        wanted.append(args.store)
    records: list = []
    if wanted or mode in (FulfillmentMode.DELIVERY, FulfillmentMode.GROUP):
        available = _resolve_delivery_records(
            client, mode, args.address_id, args.city, args.keyword, args.store)
        if wanted:
            for code in dict.fromkeys(wanted):
                matched = next((r for r in available
                                if str(r.get("storeCode")) == code), None)
                if matched:
                    records.append(matched)
                else:
                    # 用户显式指定但不在"可配送列表"里也照抓,便于诊断
                    records.append({"storeCode": code, "storeName": "",
                                    "beCode": ""})
        else:
            records = available[:max(1, args.max_stores)]
    else:
        records = _resolve_delivery_records(
            client, mode, args.address_id, args.city, args.keyword, args.store)

    if not records:
        print("✘ 没有解析到任何门店", file=sys.stderr)
        return 3

    print(f"→ 计划抓取 {len(records)} 家门店({mode.label})")
    ok = 0
    for record in records:
        if capture_store(client, args.out, mode, record):
            ok += 1

    if not args.skip_nutrition:
        try:
            raw = client.nutrition_raw()
            if raw.strip():
                path = os.path.join(args.out, "nutrition_raw.txt")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(raw)
                print(f"\n  ✔ {path}")
            else:
                print("\n  ⚠ 营养接口返回空,沿用原有快照")
        except MCPError as exc:
            print(f"\n  ⚠ 营养快照跳过:{exc}")

    print(f"\n完成:{ok}/{len(records)} 家门店抓取成功。")
    print("可用 --offline 复现同样的菜单与价格。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
