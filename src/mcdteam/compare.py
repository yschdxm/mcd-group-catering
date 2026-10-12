"""多门店比价:同一个地址下,哪家门店点同样的东西最便宜。

## 为什么这个功能有价值

实测同一个配送地址通常有 **2~3 家可配送门店**(无锡某地址有 3 家)。它们的
菜单、单价、可售状态、可用券往往不同,而团餐金额动辄几百元,
**选错门店可能多花几十块**。

人工做法是把方案在一家店排好,再换一家店重排一遍 —— 很费时间。
本模块让它在一次调用里完成:对每个候选门店独立走一遍
「取菜单 → 批量取价 → 挂营养 → 接券 → 求解 → 核价」,
然后按实付金额升序排序。

## 关键设计

* **复用** :class:`~mcdteam.orchestrator.Session` 的餐厅加载逻辑,避免两套实现;
* 每家门店的失败**互不影响**:某家门店取菜单失败时记录原因并继续比下一家,
  而不是整体失败(团餐场景下常有门店临时停业);
* 排序以 **实付金额** 为准(已核价时是门店实价),而不是本地估算;
* 预算不足、约束被放宽等风险都随方案带出来,让用户自己判断。

## 提示

单店方案的搜索强度默认是 48 次重启。比价时要跑 N 家店,为了控制耗时,
:func:`compare_stores` 会把每家的 ``effort`` 适度调低(可通过
``effort`` 参数覆盖)。这不会影响价格正确性 —— 价格始终来自
``calculate-price``,搜索强度只影响"搭配得好不好"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .mcp.context import FulfillmentMode, StoreContext
from .mcp.errors import MCPError
from .solve.constraints import CateringRequirements
from .solve.planner import CateringPlan

ProgressFn = Optional[Callable[[str], None]]

#: 比价时每家门店的默认搜索强度(比单店的 48 低,用于控制总耗时)
DEFAULT_COMPARE_EFFORT = 24


@dataclass
class StorePrice:
    """一家候选门店的比价结果。"""

    store_code: str
    store_name: str = ""
    be_code: str = ""
    business_status: Optional[bool] = None
    plan: Optional[CateringPlan] = None
    error: str = ""
    catalog_size: int = 0
    priced_size: int = 0

    @property
    def ok(self) -> bool:
        return self.plan is not None and bool(self.plan.lines)

    @property
    def evaluated(self) -> bool:
        """是否真的跑过一遍求解(用于区分「未评估」与「评估后失败」)。"""
        return self.plan is not None or bool(self.error)

    @property
    def payable_cents(self) -> int:
        if self.plan is None:
            return 0
        return self.plan.payable_cents

    @property
    def payable_yuan(self) -> float:
        return round(self.payable_cents / 100.0, 2)

    @property
    def store_label(self) -> str:
        return self.store_name or self.store_code

    def to_dict(self) -> Dict[str, Any]:
        return {
            "storeCode": self.store_code,
            "storeName": self.store_name,
            "beCode": self.be_code or None,
            "businessStatus": self.business_status,
            "ok": self.ok,
            "error": self.error or None,
            "catalogSize": self.catalog_size,
            "pricedSize": self.priced_size,
            "payableYuan": self.payable_yuan if self.ok else None,
            "perPersonYuan": self.plan.per_person_yuan if self.ok else None,
            "underBudget": self.plan.under_budget if self.ok else None,
            "totalQuantity": self.plan.total_quantity if self.ok else None,
            "items": [
                {"productName": line.item.name, "quantity": line.quantity}
                for line in (self.plan.lines if self.ok else [])
            ],
            "warnings": list(self.plan.warnings) if self.ok else [],
            "relaxations": list(self.plan.relaxations) if self.ok else [],
        }


@dataclass
class StoreComparison:
    """一次比价的完整结果。"""

    address_id: str = ""
    mode: FulfillmentMode = FulfillmentMode.DELIVERY
    headcount: int = 0
    budget_cents: int = 0
    candidates: List[StorePrice] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def ranked(self) -> List[StorePrice]:
        """按实付升序;失败的门店排在最后。"""
        usable = [c for c in self.candidates if c.ok]
        broken = [c for c in self.candidates if not c.ok]
        usable.sort(key=lambda c: (c.payable_cents, c.store_code))
        broken.sort(key=lambda c: c.store_code)
        return usable + broken

    @property
    def best(self) -> Optional[StorePrice]:
        usable = [c for c in self.candidates if c.ok]
        if not usable:
            return None
        return min(usable, key=lambda c: c.payable_cents)

    @property
    def worst(self) -> Optional[StorePrice]:
        usable = [c for c in self.candidates if c.ok]
        if not usable:
            return None
        return max(usable, key=lambda c: c.payable_cents)

    @property
    def saving_cents(self) -> int:
        """最贵与最便宜之间的差额(为 0 表示各店同价)。"""
        if not self.best or not self.worst:
            return 0
        return self.worst.payable_cents - self.best.payable_cents

    def to_dict(self) -> Dict[str, Any]:
        return {
            "addressId": self.address_id,
            "mode": self.mode.value,
            "headcount": self.headcount,
            "budgetYuan": round(self.budget_cents / 100.0, 2),
            "savingYuan": round(self.saving_cents / 100.0, 2),
            "best": self.best.to_dict() if self.best else None,
            "candidates": [c.to_dict() for c in self.ranked],
            "notes": list(self.notes),
        }


def candidate_stores(client, address_id: str,
                     be_type: int = 2) -> List[Dict[str, Any]]:
    """列出某地址下的可配送门店。"""
    return list(client.resolve_delivery_stores(address_id, be_type=be_type) or [])


def compare_stores(session, requirements: CateringRequirements, *,
                   address_id: str = "",
                   max_stores: int = 4,
                   effort: Optional[int] = None,
                   verify: bool = True,
                   progress: ProgressFn = None) -> StoreComparison:
    """对同一地址下的多家门店分别求解并排序。

    Parameters
    ----------
    session:
        :class:`~mcdteam.orchestrator.Session`;比价会复用它的客户端与营养书。
    requirements:
        需求(人数/预算/忌口/时间等)。门店由本函数决定,因此
        ``requirements.store_code`` 会被忽略。
    address_id:
        配送地址;留空则用配置里的默认地址(session 会自己解析)。
    max_stores:
        最多比价几家,默认 4(避免一次调用打太多接口)。
    """
    mode = requirements.mode
    if mode not in (FulfillmentMode.DELIVERY, FulfillmentMode.GROUP):
        raise ValueError("多门店比价只适用于外送/团餐场景(需要配送地址)")
    if effort is None:
        effort = DEFAULT_COMPARE_EFFORT

    # 解析地址
    if not address_id:
        address_id = config_address_id(session)
    if not address_id:
        raise MCPError("账号没有可用的配送地址,无法比价", tool="delivery-query-addresses")

    be_type = 6 if mode is FulfillmentMode.GROUP else 2
    stores = candidate_stores(session.client, address_id, be_type=be_type)
    if not stores:
        raise MCPError("该地址没有可配送门店", tool="delivery-query-stores")

    comparison = StoreComparison(
        address_id=address_id, mode=mode,
        headcount=requirements.headcount,
        budget_cents=requirements.budget_cents,
    )
    if len(stores) > max_stores:
        comparison.notes.append(
            f"该地址共 {len(stores)} 家可配送门店,本次只比价前 {max_stores} 家"
            "(可用 --max-stores 调整)"
        )
    stores = stores[:max_stores]

    for index, record in enumerate(stores, 1):
        store_code = str(record.get("storeCode", ""))
        store_name = str(record.get("storeName", "") or "")
        entry = StorePrice(
            store_code=store_code,
            store_name=store_name,
            be_code=str(record.get("beCode", "") or ""),
            business_status=record.get("businessStatus"),
        )
        if progress:
            progress(f"[{index}/{len(stores)}] {store_name or store_code}")

        if record.get("businessStatus") is False:
            entry.error = "门店当前不营业"
            comparison.candidates.append(entry)
            continue
        if mode.requires_be_code and not entry.be_code:
            entry.error = "门店未返回 beCode,无法外送下单"
            comparison.candidates.append(entry)
            continue

        try:
            context = StoreContext(
                store_code=store_code,
                store_name=store_name,
                mode=mode,
                be_code=entry.be_code,
                address_id=address_id,
                reservation_date=_fmt(requirements),
            )
            local = _clone_requirements(requirements, effort)
            local.store_code = store_code
            context = session._with_gm_service(context)  # noqa: SLF001 - 复用团餐助餐服务解析
            catalog = session.load_catalog_for(context)
            entry.catalog_size = len(catalog)
            entry.priced_size = len(catalog.priced())
            if entry.priced_size == 0:
                # 菜单为空或全部售罄,没有比价意义,明确记为失败而不是给个空方案
                entry.error = "该门店当前没有可售餐品"
                comparison.candidates.append(entry)
                continue
            plan = session.plan_for(context, catalog, local, verify=verify)
        except MCPError as exc:
            entry.error = str(getattr(exc, "message", exc))
            comparison.candidates.append(entry)
            continue
        except Exception as exc:  # noqa: BLE001 - 单店失败不应中断比价
            entry.error = f"{type(exc).__name__}: {exc}"
            comparison.candidates.append(entry)
            continue

        entry.plan = plan
        comparison.candidates.append(entry)

    usable = [c for c in comparison.candidates if c.ok]
    if not usable:
        comparison.notes.append("所有候选门店都没能生成方案,请检查预算与忌口条件")
    elif comparison.saving_cents <= 0:
        comparison.notes.append("各门店实付金额相同,选哪家都一样")
    return comparison


def _clone_requirements(requirements: CateringRequirements,
                        effort: int) -> CateringRequirements:
    """复制需求并调整搜索强度,避免污染调用方的对象。"""
    import copy

    clone = copy.copy(requirements)
    clone.effort = effort
    clone.required_codes = list(requirements.required_codes)
    clone.excluded_codes = list(requirements.excluded_codes)
    clone.preferences = list(requirements.preferences)
    clone.notes = list(requirements.notes)
    clone.store_code = ""
    return clone


def _fmt(requirements: CateringRequirements) -> str:
    if requirements.reserve_at:
        return requirements.reserve_at.strftime("%Y-%m-%d %H:%M")
    return ""


def config_address_id(session) -> str:
    """取默认配送地址(session 配置里的,或账号下的第一个)。"""
    configured = getattr(session.config, "address_id", "")
    if configured:
        return configured
    try:
        data = session.client.ensure_ok("delivery-query-addresses", {}).require_data() or {}
    except MCPError:
        return ""
    addresses = data.get("addresses", []) if isinstance(data, dict) else []
    if not addresses:
        return ""
    address_id = str(addresses[0].get("addressId", ""))
    session.config.address_id = address_id
    return address_id


def render_comparison(comparison: StoreComparison, *, color: bool = True) -> str:
    """把比价结果渲染成终端表格。"""
    from .report.render import Palette, _display_width

    palette = Palette(color)
    out: List[str] = []
    out.append("")
    out.append(palette.bold("🏪 多门店比价 · 同一地址,谁更便宜"))
    out.append("─" * 74)
    out.append(f"  人数 {comparison.headcount} 人"
               + (f"   ·   预算 ¥{comparison.budget_cents / 100:,.2f}"
                  if comparison.budget_cents else "")
               + f"   ·   场合 {comparison.mode.label}")
    out.append("─" * 74)
    header = f"  {'排名':<5}{'门店':<26}{'实付':>10}{'人均':>9}{'件数':>6}{'状态':>8}"
    out.append(header)
    out.append("  " + "·" * 70)

    for index, entry in enumerate(comparison.ranked, 1):
        name = entry.store_label
        if len(name) > 24:
            name = name[:23] + "…"
        name_cell = name + " " * max(0, 26 - _display_width(name))
        if entry.ok:
            amount = f"{entry.payable_yuan:>10,.2f}"
            per = f"{entry.plan.per_person_yuan:>9,.2f}"
            count = f"{entry.plan.total_quantity:>6}"
            status = "预算内" if entry.plan.under_budget else "超预算"
            line = f"  {index:<5}{name_cell}{amount}{per}{count}{status:>8}"
            out.append(palette.green(line) if index == 1 and comparison.best else line)
        else:
            reason = entry.error or "无方案"
            if len(reason) > 20:
                reason = reason[:19] + "…"
            out.append(f"  {index:<5}{name_cell}{'—':>10}{'—':>9}{'—':>6}{reason:>8}")

    out.append("  " + "·" * 70)
    best = comparison.best
    if best:
        out.append(palette.bold(
            f"  💡 最省:{best.store_label}({best.store_code}) "
            f"¥{best.payable_yuan:,.2f}"))
        if comparison.saving_cents > 0:
            out.append(palette.yellow(
                f"     比最贵的 {comparison.worst.store_label} 省 "
                f"¥{comparison.saving_cents / 100:,.2f}"))
    for note in comparison.notes:
        out.append(palette.dim(f"  · {note}"))
    out.append("─" * 74)
    out.append(palette.dim("  金额均已由麦当劳 calculate-price 接口核价;"
                           "各门店菜单与可售状态不同,清单可能不同"))
    out.append("")
    return "\n".join(out)


def render_comparison_markdown(comparison: StoreComparison) -> str:
    """比价结果的 Markdown 版本。"""
    out: List[str] = []
    out.append("# 多门店比价")
    out.append("")
    out.append(f"- 人数:{comparison.headcount} 人")
    if comparison.budget_cents:
        out.append(f"- 预算:¥{comparison.budget_cents / 100:,.2f}")
    out.append(f"- 取餐方式:{comparison.mode.label}")
    out.append("")
    out.append("| 排名 | 门店 | 编码 | 实付 | 人均 | 件数 | 状态 |")
    out.append("|---:|---|---|---:|---:|---:|---|")
    for index, entry in enumerate(comparison.ranked, 1):
        if entry.ok:
            out.append(
                f"| {index} | {entry.store_label} | {entry.store_code} | "
                f"¥{entry.payable_yuan:,.2f} | ¥{entry.plan.per_person_yuan:,.2f} | "
                f"{entry.plan.total_quantity} | "
                f"{'预算内' if entry.plan.under_budget else '超预算'} |")
        else:
            out.append(f"| {index} | {entry.store_label} | {entry.store_code} | — | — | — | "
                       f"{entry.error or '无方案'} |")
    out.append("")
    if comparison.best:
        out.append(f"**最省:{comparison.best.store_label}**"
                   f"(`{comparison.best.store_code}`)"
                   f"应付 ¥{comparison.best.payable_yuan:,.2f}")
        if comparison.saving_cents > 0:
            out.append("")
            out.append(f"比最贵的门店省 **¥{comparison.saving_cents / 100:,.2f}**。")
    if comparison.notes:
        out.append("")
        for note in comparison.notes:
            out.append(f"> {note}")
    out.append("")
    return "\n".join(out)
