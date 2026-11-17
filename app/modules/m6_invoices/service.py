"""M6 账单对账：出账、超额抵扣结转、五账核对、合计。

出账只读派生表（分段、抵扣行），因此重建一次不会改变输入；
结转分录与账单在同一事务里先删后插：同一批次重跑，账单行数、
分录与合计逐字不变；重结失败整体回滚，旧版余额与结转记录原样保留。
"""
from __future__ import annotations

from ... import config
from ..m4_proration import rounding
from . import carry as carry_mod
from . import invoice as invoice_mod
from . import reconcile


def _group(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["invoice_id"], []).append(row)
    for group in grouped.values():
        group.sort(key=lambda item: item["seq"])
    return grouped


def _covered(invoices: list[dict]) -> set[tuple[str, str]]:
    """本批次负责结算的 (账户, 期间起)：更早批次的同键分录由本批替代。"""
    return {(carry_mod.account_of(row), row["period_start"]) for row in invoices}


def _opening_lots(store, run_id: str, invoices: list[dict]) -> dict[str, list[dict]]:
    history = store.carry_opening_events(run_id, _covered(invoices))
    return carry_mod.lots_from_events(history)


def _carry_view(store, run_id: str, invoices: list[dict]) -> tuple[dict, list[dict]]:
    """页面与接口共用的结转口径：期初来自历史有效分录，本批分录回放。"""
    opening = carry_mod.balances_from_lots(_opening_lots(store, run_id, invoices))
    events = store.list_carry_events(run_id)
    return carry_mod.replay(invoices, events, opening), events


def rebuild(store, run_id: str, settings) -> dict:
    run = store.get_run(run_id) or {}
    counters = run.get("counters", {})
    subs = {sub["sub_id"]: sub for sub in store.list_subscriptions(run_id)}
    segments = store.list_segments(run_id)
    components = store.list_components(run_id)
    segment_groups = _group(segments)
    component_groups = _group(components)

    invoices: list[dict] = []
    for invoice_id, rows in sorted(segment_groups.items()):
        sub = subs.get(rows[0]["sub_id"])
        if sub is None:
            continue
        invoices.append(invoice_mod.build_invoice(
            sub, rows, component_groups.get(invoice_id, [])))

    # 超额抵扣结转：取消未用额度开单挂账户，后续期间按 FIFO 抵扣
    cancel_invoice_ids = {row["invoice_id"] for row in segments
                          if row["state"] == "cancelled"}
    opening_lots = _opening_lots(store, run_id, invoices)
    settled = carry_mod.settle(invoices, components, cancel_invoice_ids, opening_lots)
    opening = carry_mod.balances_from_lots(opening_lots)
    view = carry_mod.replay(invoices, settled["events"], opening)
    for invoice in invoices:
        cols = view["per_invoice"].get(invoice["invoice_id"], carry_mod.ZERO_COLS)
        for code, key in (("R_CARRY_OPEN", "opened"), ("R_CARRY_APPLIED", "applied")):
            if cols[key] > 0 and code not in invoice["flags"]:
                invoice["flags"].append(code)

    ledgers = reconcile.build(counters=counters, invoices=invoices,
                              segments=segments, components=components, carry=view)
    totals = reconcile.totals(counters, invoices, segments, components, carry=view)
    store.put_invoice_settlement(run_id, invoices, settled["events"], ledgers, totals)
    return {
        "summary": {
            "invoices": len(invoices),
            "period_days": totals["period_days"],
            "charge_cents": totals["charge_cents"],
            "credit_cents": totals["credit_cents"],
            "invoice_cents": totals["invoice_cents"],
            "zero_invoices": totals["zero_invoices"],
            "carry_opened_cents": totals["carry_opened_cents"],
            "carry_applied_cents": totals["carry_applied_cents"],
            "carry_balance_cents": totals["carry_balance_cents"],
            "payable_cents": totals["payable_cents"],
            "money_unit": config.MONEY_UNIT,
        },
        "ledgers": ledgers,
        "totals": totals,
        "carry": view["summary"],
    }


def _carry_cols(cols: dict) -> dict:
    return {
        "carry_in_cents": cols["carry_in"],
        "carry_applied_cents": cols["applied"],
        "carry_opened_cents": cols["opened"],
        "balance_out_cents": cols["balance_out"],
        "payable_cents": cols["payable"],
        "carry_in": rounding.cents_text(cols["carry_in"]),
        "carry_applied": rounding.cents_text(cols["applied"]),
        "carry_opened": rounding.cents_text(cols["opened"]),
        "balance_out": rounding.cents_text(cols["balance_out"]),
        "payable": rounding.cents_text(cols["payable"]),
    }


def invoice_rows(store, run_id: str) -> list[dict]:
    subs = {sub["sub_id"]: sub for sub in store.list_subscriptions(run_id)}
    invoices = store.list_invoices(run_id)
    view, _events = _carry_view(store, run_id, invoices)
    rows = []
    for row in invoices:
        sub = subs.get(row["sub_id"], {})
        cols = view["per_invoice"].get(row["invoice_id"], carry_mod.ZERO_COLS)
        rows.append({
            **row,
            "amount": rounding.cents_text(row["total_cents"]),
            "charge": rounding.cents_text(row["charge_cents"]),
            "credit": rounding.cents_text(row["credit_cents"]),
            "leap": bool(sub.get("leap", 0)),
            "range_label": f"[{row['period_start']}, {row['period_end']})",
            **_carry_cols(cols),
        })
    return rows


def detail(store, run_id: str, invoice_id: str) -> dict | None:
    row = store.get_invoice(run_id, invoice_id)
    if row is None:
        return None
    segments = store.list_segments(run_id, invoice_id)
    components = store.list_components(run_id, invoice_id)
    data = invoice_mod.detail(row, segments, components)
    view, events = _carry_view(store, run_id, store.list_invoices(run_id))
    cols = view["per_invoice"].get(invoice_id, carry_mod.ZERO_COLS)
    data["invoice"] = {**data["invoice"], **_carry_cols(cols)}
    data["carry_events"] = [
        _event_row(event) for event in events if event["invoice_id"] == invoice_id]
    return data


def _event_row(event: dict) -> dict:
    return {
        **event,
        "amount": rounding.cents_text(event["amount_cents"]),
        "balance": rounding.cents_text(event["balance_after"]),
        "kind_label": config.CARRY_KIND_LABELS.get(event["kind"], event["kind"]),
    }


def carry_overview(store, run_id: str) -> dict:
    invoices = store.list_invoices(run_id)
    view, events = _carry_view(store, run_id, invoices)
    balances = [
        {"account": account, **cols} for account, cols in sorted(view["accounts"].items())
    ]
    return {
        "balances": balances,
        "events": [_event_row(event) for event in events],
        "summary": view["summary"],
    }


def overview(store, run_id: str) -> dict:
    return {
        "invoices": invoice_rows(store, run_id),
        "ledgers": store.list_reconciliations(run_id),
        "totals": store.list_totals(run_id),
        "carry": carry_overview(store, run_id),
    }
