"""报告输出:终端卡片、Markdown 提案、群聊短卡片。

三种受众,三种粒度:

``render_terminal``
    给统筹者本人看:完整明细、营养、约束让位提醒、下一步动作。
``render_markdown``
    给老板/行政审批看:可归档、可贴进 IM,含数据来源与免责声明。
``render_chat_card``
    给同事群看:一段可以直接复制的文本,只有结论和亮点。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from ..solve.constraints import CateringRequirements
from ..solve.planner import CateringPlan

_WIDTH = 68


class Palette:
    """极简 ANSI 配色,``enabled=False`` 时全部退化为空串。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)


def _rule(char: str = "─") -> str:
    return char * _WIDTH


def _money(cents: int) -> str:
    return f"¥{cents / 100:,.2f}"


def _per_person_line(plan: CateringPlan) -> str:
    """用「每 N 人一份」的方式表达配比,比纯数字更容易被行政看懂。"""
    people = max(1, plan.headcount)
    bits: List[str] = []
    for label, count in plan.role_counts().items():
        per = people / count if count else 0
        if per <= 1.15:
            bits.append(f"{label} 1 份/人")
        else:
            bits.append(f"{label} 每 {per:.1f} 人 1 份")
    return "  ·  ".join(bits) if bits else "—"


def render_terminal(plan: CateringPlan, requirements: CateringRequirements, *,
                    color: bool = True, source_note: str = "") -> str:
    """终端卡片。"""
    p = Palette(color)
    out: List[str] = []
    out.append("")
    out.append(p.bold("🍟 麦麦团餐统筹官 · 方案已生成"))
    out.append(_rule("━"))
    head = f"门店 {plan.store_name or plan.store_code}"
    if plan.store_code and plan.store_name:
        head += f"({plan.store_code})"
    out.append(f"  {head}")
    out.append(f"  人数 {plan.headcount} 人   ·   场合 {requirements.mode.label}"
               + (f"   ·   时段 {requirements.meal_time}" if requirements.meal_time else ""))
    if requirements.reserve_at:
        out.append(f"  期望时间 {requirements.reserve_at:%Y-%m-%d %H:%M}")
    out.append(_rule())

    out.append(p.bold("  📋 采购清单"))
    out.append(f"  {'餐品':<22}{'角色':<6}{'单价':>8}{'数量':>5}{'小计':>10}")
    out.append("  " + "·" * (_WIDTH - 4))
    for line in plan.lines:
        name = line.item.name
        if len(name) > 20:
            name = name[:19] + "…"
        name_cell = name + " " * max(0, 22 - _display_width(name))
        role_cell = line.item.role.label + " " * max(0, 6 - _display_width(line.item.role.label))
        out.append(
            f"  {name_cell}{role_cell}"
            f"{line.item.price_yuan:>8.2f}{line.quantity:>5}{line.subtotal / 100:>10.2f}"
        )
    derived = [line.item.name for line in plan.lines if line.item.nutrition_derived]
    if derived:
        out.append(p.dim("  ※ 以下套餐的营养由 query-meal-detail 的组成累加得出:"))
        for name in derived:
            out.append(p.dim(f"    ※ {name}"))
    out.append("  " + "·" * (_WIDTH - 4))
    out.append(f"  {'合计':<32}{plan.total_quantity:>5}{plan.subtotal_yuan:>10.2f}")

    out.append(_rule())
    out.append(p.bold("  💰 结算"))
    out.append(f"  商品原价            {_money(plan.subtotal_cents):>12}")
    if plan.discount_cents:
        out.append(p.green(f"  优惠抵扣            {-plan.discount_cents / 100:>12,.2f}"))
    if plan.delivery_fee_cents:
        out.append(f"  配送费              {_money(plan.delivery_fee_cents):>12}")
    total_color = p.green if plan.under_budget else p.red
    out.append(total_color(f"  应付总额            {_money(plan.payable_cents):>12}"))
    out.append(f"  人均                {_money(int(round(plan.per_person_cents))):>12}")
    if plan.budget_cents:
        if plan.under_budget:
            out.append(p.green(f"  预算余量            {_money(plan.budget_cents - plan.payable_cents):>12}"))
        else:
            out.append(p.red(f"  超预算              {_money(plan.payable_cents - plan.budget_cents):>12}"))
    for coupon in plan.coupons_applied:
        out.append(p.cyan(f"  ✔ 已用券 {coupon.title}"))

    if plan.verified_by_api:
        out.append(p.dim("  (金额已由麦当劳 calculate-price 接口实时回算)"))
    else:
        out.append(p.yellow("  (金额为本地按单价估算,未经服务端回算)"))
    if plan.budget_cents:
        rate = plan.payable_cents / plan.budget_cents * 100
        out.append(p.dim(f"  预算使用率 {rate:.0f}%"))

    out.append(_rule())
    out.append(p.bold("  👥 每人怎么分"))
    out.append("  " + _per_person_line(plan))

    totals = plan.nutrition_totals()
    out.append(_rule())
    out.append(p.bold("  🥗 营养参考(整单 / 人均)"))
    out.append(
        f"  热量 {totals['kcal']:>8,.0f} kcal  /  {plan.per_person('kcal'):>6,.0f} kcal"
    )
    out.append(
        f"  蛋白质 {totals['protein']:>6,.0f} g    /  {plan.per_person('protein'):>6,.0f} g"
    )
    out.append(
        f"  脂肪 {totals['fat']:>8,.0f} g    /  {plan.per_person('fat'):>6,.0f} g"
    )
    out.append(
        f"  碳水 {totals['carbohydrate']:>8,.0f} g    /  {plan.per_person('carbohydrate'):>6,.0f} g"
    )
    out.append(
        f"  钠 {totals['sodium']:>10,.0f} mg   /  {plan.per_person('sodium'):>6,.0f} mg"
    )

    if requirements.hard_rule_summary() or requirements.soft_rule_summary():
        out.append(_rule())
        out.append(p.bold("  🚧 约束核对"))
        if requirements.hard_rule_summary():
            out.append("  硬性(已强制满足): " + "、".join(requirements.hard_rule_summary()))
        if requirements.soft_rule_summary():
            out.append("  软性(已尽量满足): " + "、".join(requirements.soft_rule_summary()))

    if plan.relaxations or plan.warnings:
        out.append(_rule())
        out.append(p.bold("  ⚠️ 需要你拍板的地方"))
        for note in plan.relaxations:
            out.append(p.red(f"  · {note}"))
        for note in plan.warnings:
            out.append(p.yellow(f"  · {note}"))

    out.append(_rule())
    out.append(p.bold("  ▶ 下一步"))
    out.append("  1. 复核清单与忌口;2. 提交下单确认;3. 下单后把支付链接发给同事")
    if source_note:
        out.append(p.dim(f"  数据来源:{source_note}"))
    out.append(p.dim("  本方案由第三方 Skill 生成,非麦当劳官方产品;营养数据仅供参考,"
                     "不构成医疗或营养建议。"))
    out.append("")
    return "\n".join(out)


def _display_width(text: str) -> int:
    """估算终端显示宽度(中日韩字符按 2 列计)。"""
    width = 0
    for char in text:
        width += 2 if ord(char) > 0x2E7F else 1
    return width


def render_markdown(plan: CateringPlan, requirements: CateringRequirements, *,
                    source_note: str = "", generated_at: str = "",
                    context: Optional[Dict[str, Any]] = None) -> str:
    """生成可归档的 Markdown 提案。"""
    out: List[str] = []
    out.append(f"# 团餐采购提案 · {plan.store_name or plan.store_code}")
    out.append("")
    if generated_at:
        out.append(f"> 生成时间:{generated_at}　数据来源:{source_note or '麦当劳 MCP 实时接口'}")
        out.append("")

    out.append("## 一、需求与理解")
    out.append("")
    out.append("| 项目 | 内容 |")
    out.append("|---|---|")
    out.append(f"| 人数 | {plan.headcount} 人"
               + (f"(含儿童 {requirements.kids} 人)" if requirements.kids else "") + " |")
    out.append(f"| 预算 | ¥{requirements.budget_yuan:,.2f}"
               + (f"(人均 ¥{requirements.per_person_yuan:,.2f})" if requirements.per_person_yuan else "")
               + " |")
    out.append(f"| 取餐方式 | {requirements.mode.label} |")
    if requirements.reserve_at:
        out.append(f"| 期望时间 | {requirements.reserve_at:%Y-%m-%d %H:%M} |")
    if requirements.meal_time:
        out.append(f"| 用餐时段 | {requirements.meal_time} |")
    if requirements.hard_rule_summary():
        out.append(f"| 硬性忌口 | {'、'.join(requirements.hard_rule_summary())} |")
    if requirements.soft_rule_summary():
        out.append(f"| 偏好 | {'、'.join(requirements.soft_rule_summary())} |")
    out.append("")

    out.append("## 二、采购清单")
    out.append("")
    out.append("| # | 餐品 | 角色 | 单价 | 数量 | 小计 | 单件热量 |")
    out.append("|---:|---|---|---:|---:|---:|---:|")
    for index, line in enumerate(plan.lines, 1):
        kcal = f"{line.item.kcal:,.0f} kcal" if line.item.kcal is not None else "—"
        out.append(
            f"| {index} | {line.item.name} | {line.item.role.label} | "
            f"¥{line.item.price_yuan:.2f} | {line.quantity} | "
            f"¥{line.subtotal / 100:,.2f} | {kcal} |"
        )
    out.append(f"| | **合计** | | | **{plan.total_quantity}** | "
               f"**¥{plan.subtotal_yuan:,.2f}** | |")
    out.append("")

    out.append("## 三、费用")
    out.append("")
    out.append("| 项目 | 金额 |")
    out.append("|---|---:|")
    out.append(f"| 商品原价 | ¥{plan.subtotal_yuan:,.2f} |")
    if plan.discount_cents:
        out.append(f"| 优惠抵扣 | -¥{plan.discount_yuan:,.2f} |")
    if plan.delivery_fee_cents:
        out.append(f"| 配送费 | ¥{plan.delivery_fee_cents / 100:,.2f} |")
    out.append(f"| **应付总额** | **¥{plan.payable_yuan:,.2f}** |")
    out.append(f"| 人均 | ¥{plan.per_person_yuan:,.2f} |")
    if plan.budget_cents:
        delta = plan.budget_delta_yuan
        label = "预算余量" if delta >= 0 else "超出预算"
        out.append(f"| {label} | ¥{abs(delta):,.2f} |")
    out.append("")
    if plan.coupons_applied:
        out.append("**使用的优惠**")
        out.append("")
        for coupon in plan.coupons_applied:
            out.append(f"- {coupon.title}（截止 {coupon.valid_to:%Y-%m-%d %H:%M}）"
                       if coupon.valid_to else f"- {coupon.title}")
        out.append("")

    totals = plan.nutrition_totals()
    out.append("## 四、营养参考")
    out.append("")
    out.append("| 指标 | 整单 | 人均 |")
    out.append("|---|---:|---:|")
    out.append(f"| 热量 | {totals['kcal']:,.0f} kcal | {plan.per_person('kcal'):,.0f} kcal |")
    out.append(f"| 蛋白质 | {totals['protein']:,.0f} g | {plan.per_person('protein'):,.0f} g |")
    out.append(f"| 脂肪 | {totals['fat']:,.0f} g | {plan.per_person('fat'):,.0f} g |")
    out.append(f"| 碳水 | {totals['carbohydrate']:,.0f} g | {plan.per_person('carbohydrate'):,.0f} g |")
    out.append(f"| 钠 | {totals['sodium']:,.0f} mg | {plan.per_person('sodium'):,.0f} mg |")
    out.append("")
    counted = int(totals.get("_counted_quantity", 0))
    if counted < plan.total_quantity:
        out.append(f"> 注:{plan.total_quantity - counted} 件餐品未匹配到官方营养数据,"
                   "上表为下限值。营养素数据来自麦当劳 MCP `list-nutrition-foods`。")
        out.append("")

    if plan.relaxations or plan.warnings:
        out.append("## 五、需要确认的事项")
        out.append("")
        for note in plan.relaxations:
            out.append(f"- ⚠️ {note}")
        for note in plan.warnings:
            out.append(f"- {note}")
        out.append("")

    if context:
        out.append("## 附:调用轨迹")
        out.append("")
        out.append("```json")
        out.append(json.dumps(context, ensure_ascii=False, indent=2))
        out.append("```")
        out.append("")

    out.append("---")
    out.append("")
    out.append("本提案由「麦麦团餐统筹官」(第三方 Skill,非麦当劳官方产品)基于麦当劳 MCP "
               "实时数据生成;餐品价格、可售状态以门店实时结果为准,营养信息仅供参考,"
               "不构成医疗或营养建议。")
    out.append("")
    return "\n".join(out)


def render_chat_card(plan: CateringPlan, requirements: CateringRequirements) -> str:
    """一段可以直接粘进同事群的短卡片。"""
    highlights: List[str] = []
    for line in plan.lines:
        if line.quantity >= 2 or line.item.role.value in ("main", "combo"):
            highlights.append(f"{line.item.name}×{line.quantity}")
    out: List[str] = []
    out.append(f"🍟【{plan.store_name or '麦当劳'} 团餐已排好】")
    out.append(f"👥 {plan.headcount} 人 　💰 应付 ¥{plan.payable_yuan:,.2f}(人均 ¥{plan.per_person_yuan:,.2f})")
    if plan.budget_cents and plan.under_budget:
        out.append(f"✅ 预算内,还剩 ¥{plan.budget_delta_yuan:,.2f}")
    if plan.discount_cents:
        out.append(f"🎟️ 已叠加优惠省下 ¥{plan.discount_yuan:,.2f}")
    if requirements.hard_rule_summary():
        out.append("🚫 已避开:" + "、".join(requirements.hard_rule_summary()))
    out.append("📋 " + " / ".join(highlights[:10]))
    if requirements.reserve_at:
        out.append(f"⏰ 预计 {requirements.reserve_at:%m-%d %H:%M} 送达")
    return "\n".join(out)
