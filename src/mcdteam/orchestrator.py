"""端到端统筹流程:鉴权 → 选店 → 取菜单 → 取价 → 取营养 → 接券 → 求解 → 回算。

把这段流程单独抽出来,是为了让 CLI、测试和未来的 MCP Server 封装共用同一实现,
避免「命令行能跑、接口跑不通」的常见分裂。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .config import AppConfig
from .data.coupons import Coupon, Promotion, parse_coupons, parse_promotions
from .data.menu import MenuCatalog, build_catalog, collect_price_map
from .data.nutrition import NutritionBook
from .mcp.client import MCPClient
from .mcp.context import FulfillmentMode, StoreContext
from .mcp.errors import MCPBusinessError, MCPError
from .solve.constraints import CateringRequirements, parse_requirements
from .solve.planner import (
    CateringPlan,
    CateringPlanner,
    enrich_plan_nutrition,
    fit_plan_to_budget,
    verify_plan_with_api,
)

ProgressFn = Optional[Callable[[str], None]]


@dataclass
class Session:
    """一次团餐统筹会话。"""

    config: AppConfig
    progress: ProgressFn = None
    client: MCPClient = field(init=False)
    catalog: Optional[MenuCatalog] = None
    store_context: Optional[StoreContext] = None
    coupons: List[Coupon] = field(default_factory=list)
    promotions: List[Promotion] = field(default_factory=list)
    nutrition_book: Optional[NutritionBook] = None
    trace: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.client = MCPClient(
            self.config.token,
            endpoint=self.config.endpoint,
            timeout=self.config.timeout,
            snapshot_dir=self.config.fixture_dir,
            allow_snapshot=True,
            offline=self.config.offline,
            verbose=self.config.verbose,
        )

    # ------------------------------------------------------------------ 工具
    def _say(self, message: str) -> None:
        if self.config.verbose and self.progress:
            self.progress(message)

    def _record(self, step: str, **payload: Any) -> None:
        entry = {"step": step}
        entry.update(payload)
        self.trace.append(entry)

    # ---------------------------------------------------------------- 门店层
    def resolve_store(self, requirements: CateringRequirements) -> StoreContext:
        """把需求解析成 :class:`StoreContext`。

        优先级:显式 storeCode > 配置里的 storeCode > 按城市/关键词搜索。
        """
        store_code = requirements.store_code or self.config.store_code
        mode = requirements.mode
        be_code = self.config.be_code

        if store_code:
            if mode.requires_be_code and not be_code:
                if self.config.offline:
                    be_code = "offline"
                    self._record("resolve_store", note="离线模式使用占位 beCode")
                else:
                    be_code = self._lookup_be_code(store_code, mode, requirements)
            context = StoreContext(
                store_code=store_code, mode=mode, be_code=be_code,
                address_id=self.config.address_id,
                reservation_date=_fmt(requirements.reserve_at),
                city=requirements.city or self.config.city,
            )
            self._record("resolve_store", storeCode=store_code, source="explicit")
            return self._with_gm_service(context)

        if self.config.offline:
            raise MCPError(
                "离线模式必须提供 storeCode(可用 --store 或在配置中设置 MCD_STORE_CODE)",
                tool="resolve_store",
            )

        records = self._search_stores(requirements)
        if not records:
            raise MCPError(
                f"未在「{requirements.city or '指定城市'}」找到可用门店,"
                "请补充 --city/--keyword 或直接指定 --store",
                tool="query-nearby-stores",
            )
        record = records[0]
        context = StoreContext.from_store_record(
            record, mode, reservation_date=_fmt(requirements.reserve_at))
        self._record("resolve_store", storeCode=context.store_code,
                     storeName=context.store_name, source="search")
        if self.progress:
            self.progress(f"已选门店:{context.store_name or context.store_code}")
        return self._with_gm_service(context)

    def _with_gm_service(self, context: StoreContext) -> StoreContext:
        """企业团餐必须先取助餐服务码,否则 calculate-price 会被拒绝。

        实测错误文案是「团餐助餐服务参数缺失」,而 `query-meals` 在没有它时
        却仍能返回菜单,所以很容易在取价阶段才炸。这里在选店后立即补齐。
        """
        if context.be_type != 6 or context.gm_service_code:
            return context
        try:
            services = self.client.meal_assistance(
                context.store_code, order_type=context.order_type, be_type=6,
                be_code=context.be_code)
        except MCPError as exc:
            self._say(f"助餐服务查询失败(团餐取价可能被拒):{exc}")
            return context

        chosen = ""
        for service in services or []:
            if not isinstance(service, dict):
                continue
            code = str(service.get("gmServiceCode") or "")
            if not code or service.get("enable") is False:
                continue
            if service.get("selected"):
                chosen = code
                break
            if not chosen:
                chosen = code
        if chosen:
            self._record("query-meal-assistance", gmServiceCode=chosen)
            if self.progress:
                self.progress(f"团餐助餐服务 = {chosen}")
            return context.with_gm_service(chosen)
        return context

    def _search_stores(self, requirements: CateringRequirements) -> List[Dict[str, Any]]:
        city = requirements.city or self.config.city
        keyword = requirements.keyword
        if requirements.mode is FulfillmentMode.PICKUP:
            return self.client.resolve_store_code(city, keyword, be_type=1)
        if requirements.mode is FulfillmentMode.DRIVE_THRU:
            return self.client.resolve_store_code(city, keyword, be_type=5)
        # 外送 / 团餐:先拿地址,再查可配送门店
        addresses = self.client.ensure_ok("delivery-query-addresses", {}).require_data() or {}
        address_list = addresses.get("addresses", []) if isinstance(addresses, dict) else []
        if not address_list:
            raise MCPError("当前账号没有配送地址,请先在麦当劳 App 或通过 MCP 创建地址",
                           tool="delivery-query-addresses")
        address_id = self.config.address_id or str(address_list[0].get("addressId", ""))
        self.config.address_id = address_id
        be_type = 6 if requirements.mode is FulfillmentMode.GROUP else 2
        return self.client.resolve_delivery_stores(address_id, be_type=be_type)

    def _lookup_be_code(self, store_code: str, mode: FulfillmentMode,
                        requirements: CateringRequirements) -> str:
        records = self._search_stores(requirements)
        for record in records:
            if str(record.get("storeCode")) == str(store_code):
                code = str(record.get("beCode") or "")
                if code:
                    return code
        if records:
            code = str(records[0].get("beCode") or "")
            if code:
                return code
        raise MCPError(
            f"{mode.label} 场景需要 beCode,但未能从门店接口获取;"
            "请用 --be-code 显式指定", tool="query-nearby-stores")

    # ---------------------------------------------------------------- 菜单层
    def load_catalog(self, context: StoreContext, *, max_items: int = 0) -> MenuCatalog:
        """取菜单 → 批量取价 → 挂营养。"""
        if self.progress:
            self.progress("拉取门店菜单(query-meals)")
        payload = self.client.query_meals(
            context.store_code, order_type=context.order_type, be_type=context.be_type,
            be_code=context.be_code, reservation_date=context.reservation_date,
        )
        if not isinstance(payload, dict) or not payload.get("categories"):
            raise MCPError(
                "query-meals 未返回可售餐品(categories 为空)。"
                "请确认 storeCode 真实存在、且与 orderType/beType/beCode 匹配;"
                "若在离线模式,请检查 fixtures/query-meals.<storeCode>.json 是否存在",
                tool="query-meals",
            )
        codes = [str(meal.get("code", "")) for group in payload.get("categories", []) or []
                 for meal in group.get("meals", []) or []]
        codes = [c for c in dict.fromkeys(codes) if c]
        if max_items:
            codes = codes[:max_items]
        self._record("query-meals", storeCode=context.store_code, codes=len(codes))

        # 团餐在这之后再补一次助餐服务码:resolve_store 可能来自缓存/显式 storeCode
        context = self._with_gm_service(context)
        self.store_context = context

        price_map = (self._offline_price_map(context.store_code, context.order_type,
                                            context.be_type)
                     if self.config.offline
                     else collect_price_map(self.client, context, codes, progress=self.progress))
        self._record("calculate-price", requested=len(codes), priced=len(price_map))

        book = self.load_nutrition()
        catalog = build_catalog(
            payload, price_map, book,
            store_code=context.store_code, store_name=context.store_name,
            source="snapshot" if self.client.snapshot_hits else "live",
        )
        self.catalog = catalog
        if self.progress:
            stats = catalog.stats()
            self.progress(
                f"菜单就绪:{stats['priced']}/{stats['total']} 件有价格,"
                f"营养覆盖 {stats['nutritionRate'] * 100:.0f}%"
            )
        return catalog

    def _offline_price_map(self, store_code: str, order_type: int = 1,
                           be_type: int = 1) -> Dict[str, Dict[str, Any]]:
        """离线模式下从 ``price-map.store{code}.json`` 读取价格快照。

        快照里记录了抓取时的 ``orderType``/``beType``;若与本次请求不一致,
        会在调用方的 ``warnings`` 里体现(到店价与外送价本来就不同)。
        """
        path = os.path.join(self.config.fixture_dir, f"price-map.store{store_code}.json")
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        captured_type = payload.get("orderType")
        if captured_type is not None and int(captured_type) != int(order_type):
            self._record(
                "price-snapshot-mismatch",
                capturedOrderType=captured_type, requestedOrderType=order_type,
                note="快照价格来自不同的点餐方式,金额仅供演示,请以实时接口为准",
            )
        price_map: Dict[str, Dict[str, Any]] = {}
        for product in payload.get("products", []) or []:
            code = str(product.get("productCode", ""))
            if code:
                price_map[code] = {
                    "productName": product.get("productName", ""),
                    "subtotal": int(product.get("price") or 0),
                }
        return price_map

    def load_nutrition(self, retries: int = 3) -> NutritionBook:
        """取营养表。实测该接口偶发返回空内容,因此内置重试 + 快照兜底。"""
        if self.nutrition_book is not None:
            return self.nutrition_book

        raw = ""
        last_error: Optional[Exception] = None
        for attempt in range(max(1, retries)):
            try:
                raw = self.client.nutrition_raw()
            except MCPError as exc:
                last_error = exc
                raw = ""
            if raw and raw.strip():
                break
            if self.config.offline:
                break
            self._say(f"营养接口返回空,重试 {attempt + 1}/{retries}")
        if not raw.strip():
            snapshot = os.path.join(self.config.fixture_dir, "nutrition_raw.txt")
            if os.path.exists(snapshot):
                with open(snapshot, "r", encoding="utf-8-sig") as handle:
                    raw = handle.read()
                self._record("list-nutrition-foods", source="snapshot")
                self.client.snapshot_hits.append(f"list-nutrition-foods <- {snapshot}")
            elif last_error:
                raise last_error
            else:
                raise MCPError("营养数据为空且没有可用快照", tool="list-nutrition-foods")

        self.nutrition_book = NutritionBook.from_raw(raw)
        if not self.trace or self.trace[-1].get("step") != "list-nutrition-foods":
            self._record("list-nutrition-foods", entries=len(self.nutrition_book))
        return self.nutrition_book

    def load_coupons(self, context: StoreContext) -> List[Coupon]:
        """取门店券 + 卡包券,合并去重。"""
        raw: List[Dict[str, Any]] = []
        try:
            raw.extend(self.client.store_coupons(
                context.store_code, order_type=context.order_type,
                be_type=context.be_type, be_code=context.be_code) or [])
        except MCPError as exc:
            self._say(f"门店券查询失败,已跳过:{exc}")
        try:
            wallet = self.client.ensure_ok("query-my-coupons",
                                           {"page": "1", "pageSize": "200"}).require_data() or {}
            if isinstance(wallet, dict):
                raw.extend(wallet.get("coupons", []) or [])
        except MCPError as exc:
            self._say(f"卡包券查询失败,已跳过:{exc}")
        self.coupons = parse_coupons(raw)
        self._record("query-store-coupons", coupons=len(self.coupons))
        return self.coupons

    def load_promotions(self, context: StoreContext) -> List[Promotion]:
        """企业团餐专属满减/满折规则(仅 beType=6 有效)。"""
        if context.be_type != 6 or not context.be_code:
            self.promotions = []
            return self.promotions
        try:
            raw = self.client.promotions(
                context.store_code, context.be_code,
                reservation_date=context.reservation_date)
        except MCPError as exc:
            self._say(f"团餐促销规则查询失败,已跳过:{exc}")
            raw = []
        self.promotions = parse_promotions(raw)
        self._record("query-promotions", promotions=len(self.promotions))
        return self.promotions

    # ------------------------------------------------------------------ 求解
    def plan(self, requirements: CateringRequirements, *,
             verify: bool = True) -> CateringPlan:
        context = self.store_context or self.resolve_store(requirements)
        self.store_context = context
        # 把实际生效的场合回写到需求上,报告里的「场合」才会与真实调用一致
        if context.mode is not requirements.mode:
            requirements.mode = context.mode
        catalog = self.catalog or self.load_catalog(context)
        self.load_coupons(context)
        self.load_promotions(context)

        planner = CateringPlanner(
            catalog, requirements,
            coupons=self.coupons, promotions=self.promotions,
            progress=self.progress,
        )
        if self.progress:
            self.progress("开始方案搜索")
        plan = planner.solve()
        plan.store_name = plan.store_name or context.store_name
        if not self.config.offline:
            if verify:
                verify_plan_with_api(self.client, context, plan, progress=self.progress)
                # 门店实价可能高于本地快照价,这里自动收敛回预算内
                if plan.budget_cents and plan.payable_cents > plan.budget_cents:
                    plan = fit_plan_to_budget(self.client, context, plan, progress=self.progress)
            # 缺营养数据的含多件餐品(套餐/乐园餐)用 query-meal-detail 拆解组成累加。
            # 这一步与价格核验独立:即使调用方关了核价,营养也应该尽量补全。
            if self.nutrition_book is not None:
                plan = enrich_plan_nutrition(
                    self.client, context, plan, self.nutrition_book, progress=self.progress)
        self._record(
            "plan",
            payableYuan=plan.payable_yuan,
            items=plan.total_quantity,
            underBudget=plan.under_budget,
        )
        return plan

    # ------------------------------------------------------------------ 元信息
    def snapshot_note(self) -> str:
        if self.client.snapshot_hits:
            uniq = sorted({hit.split(" <- ")[0] for hit in self.client.snapshot_hits})
            return f"离线快照兜底:{', '.join(uniq)}"
        return "麦当劳 MCP 实时接口"

    def trace_dict(self) -> Dict[str, Any]:
        return {
            "generatedAt": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "config": self.config.describe(),
            "storeContext": self.store_context.to_dict() if self.store_context else None,
            "calls": self.client.call_log,
            "snapshots": self.client.snapshot_hits,
            "steps": self.trace,
        }


def _fmt(moment: Optional[_dt.datetime]) -> str:
    return moment.strftime("%Y-%m-%d %H:%M") if moment else ""


_MOMENT_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M",
    "%m-%d %H:%M", "%H:%M",
)


def _parse_moment(text: str) -> Optional[_dt.datetime]:
    """解析 ``--at`` 的时间写法,支持省略日期(自动取下一个未来时刻)。"""
    raw = (text or "").strip()
    if not raw:
        return None
    now = _dt.datetime.now()
    for fmt in _MOMENT_FORMATS:
        try:
            parsed = _dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt in ("%m-%d %H:%M", "%H:%M"):
            parsed = parsed.replace(year=now.year)
            if fmt == "%H:%M":
                parsed = parsed.replace(month=now.month, day=now.day)
            if parsed <= now:
                parsed += _dt.timedelta(days=1)
        return parsed
    return None


def build_requirements(text: str, **overrides: Any) -> CateringRequirements:
    """解析自然语言需求并叠加命令行显式覆盖。"""
    requirements = parse_requirements(text)
    for key, value in overrides.items():
        if value is None:
            continue
        if key == "mode" and value:
            requirements.mode = FulfillmentMode.parse(value)
        elif key == "reserve_at" and value:
            parsed = value if isinstance(value, _dt.datetime) else _parse_moment(str(value))
            if parsed is not None:
                requirements.reserve_at = parsed
        elif key == "preferences" and value:
            for pref in value:
                if pref not in requirements.preferences:
                    requirements.preferences.append(pref)
        elif key == "required_codes" and value:
            requirements.required_codes.extend(
                code for code in value if code not in requirements.required_codes)
        else:
            setattr(requirements, key, value)
    return requirements
