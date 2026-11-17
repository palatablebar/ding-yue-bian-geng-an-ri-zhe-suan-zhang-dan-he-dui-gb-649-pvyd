"""超额抵扣结转结算：取消未用额度记结转余额，逐期抵扣后续账单。

结算规则（本期应收不为负 + 未用金额完整保留 + 余额逐期可核）：

1. 账单额 = 实际用量，「预缴 − 抵扣 + 补收」恒等式保证本期应收不为负；
   结转抵扣同样不允许超过账单额，应付 = 账单额 − 结转抵扣 ≥ 0。
2. 含取消段的账单：未用额度 = max(0, 整期预缴 − 账单额) 记一条「结转开单」，
   挂在账户名下，不退款、不抹零，也不抵同一期间的其他账单。
3. 后续期间该账户的账单按 (期间起, 账单号) 依次用余额抵扣；抵扣按 FIFO
   匹配来源开单，每笔分录都记来源账单与抵扣后余额，余额的每一分都有逐期依据。
4. 分录是 run 级派生数据：重算在同一事务里先删后插；跨批次期初只数
   「更早 done 批次、同一 (账户, 期间) 取最新批次」的分录。同一结算重跑
   不会重复结转或冲销，失败回滚后旧版余额与分录原样保留。
"""
from __future__ import annotations

from itertools import groupby

OPEN = "open"
APPLY = "apply"

ZERO_COLS = {"carry_in": 0, "applied": 0, "opened": 0, "balance_out": 0, "payable": 0}


def account_of(invoice: dict) -> str:
    """结转余额挂在账户名下；账户缺失时退订阅号，不让余额落空。"""
    return invoice["account"] or invoice["sub_id"]


def lots_from_events(events: list[dict]) -> dict[str, list[dict]]:
    """把历史分录回放到「剩余额度批次」：开单入队，抵扣按 FIFO 从队首消耗。"""
    lots: dict[str, list[dict]] = {}
    for event in events:
        bucket = lots.setdefault(event["account"], [])
        amount = int(event["amount_cents"])
        if event["kind"] == OPEN:
            bucket.append({"source_invoice": event["invoice_id"],
                           "period_start": event["period_start"],
                           "remaining": amount})
            continue
        take = -amount
        while take > 0 and bucket:
            head = bucket[0]
            use = min(head["remaining"], take)
            head["remaining"] -= use
            take -= use
            if head["remaining"] <= 0:
                bucket.pop(0)
    return {account: [lot for lot in bucket if lot["remaining"] > 0]
            for account, bucket in lots.items()}


def balances_from_lots(lots: dict[str, list[dict]]) -> dict[str, int]:
    return {account: sum(lot["remaining"] for lot in bucket)
            for account, bucket in lots.items()}


def settle(invoices: list[dict], components: list[dict],
           cancel_invoice_ids: set[str],
           opening_lots: dict[str, list[dict]]) -> dict:
    """对本批次账单做结转结算，返回新分录与期末余额（只算不写库）。

    幂等性由调用方保证：期初只读历史批次的有效分录，本批分录先删后插。
    """
    base_by_invoice: dict[str, int] = {}
    for row in components:
        if row["kind"] == "charge" and row["source"] == "base":
            base_by_invoice[row["invoice_id"]] = (
                base_by_invoice.get(row["invoice_id"], 0) + int(row["cents"]))

    active = {account: [dict(lot) for lot in bucket]
              for account, bucket in opening_lots.items()}
    pending: dict[str, list[dict]] = {}
    events: list[dict] = []

    def balance_of(account: str) -> int:
        return (sum(lot["remaining"] for lot in active.get(account, []))
                + sum(lot["remaining"] for lot in pending.get(account, [])))

    ordered = sorted(invoices, key=lambda row: (row["period_start"], row["invoice_id"]))
    for period_start, group in groupby(ordered, key=lambda row: row["period_start"]):
        for invoice in group:
            account = account_of(invoice)
            invoice_id = invoice["invoice_id"]
            total = int(invoice["total_cents"])
            # 1) 历史余额抵本期应付（FIFO），抵扣不超过账单额，应付不为负
            budget = min(total, sum(lot["remaining"]
                                    for lot in active.get(account, [])))
            apply_seq = 0
            while budget > 0 and active.get(account):
                lot = active[account][0]
                take = min(lot["remaining"], budget)
                lot["remaining"] -= take
                budget -= take
                if lot["remaining"] <= 0:
                    active[account].pop(0)
                apply_seq += 1
                events.append({
                    "seq": len(events),
                    "event_id": f"CF-APPLY-{invoice_id}-{apply_seq}",
                    "account": account,
                    "sub_id": invoice["sub_id"],
                    "invoice_id": invoice_id,
                    "period_start": period_start,
                    "kind": APPLY,
                    "amount_cents": -take,
                    "balance_after": balance_of(account),
                    "source_invoice": lot["source_invoice"],
                    "note": "结转余额抵扣本期应付",
                })
            # 2) 取消账单的未用额度开单，挂到后续期间（本期间不可用）
            if invoice_id in cancel_invoice_ids:
                open_amount = max(0, base_by_invoice.get(invoice_id, 0) - total)
                if open_amount > 0:
                    pending.setdefault(account, []).append({
                        "source_invoice": invoice_id,
                        "period_start": period_start,
                        "remaining": open_amount,
                    })
                    events.append({
                        "seq": len(events),
                        "event_id": f"CF-OPEN-{invoice_id}",
                        "account": account,
                        "sub_id": invoice["sub_id"],
                        "invoice_id": invoice_id,
                        "period_start": period_start,
                        "kind": OPEN,
                        "amount_cents": open_amount,
                        "balance_after": balance_of(account),
                        "source_invoice": invoice_id,
                        "note": "取消未用额度结转后续期间",
                    })
        # 期间边界：本期间开单的额度只给更晚的期间用
        for account, bucket in pending.items():
            active.setdefault(account, []).extend(bucket)
        pending.clear()

    balances = {account: balance_of(account) for account in active}
    return {"events": events, "balances": balances}


def check_balance_chain(events: list[dict], opening: dict[str, int]) -> bool:
    """逐条回放分录，核对每条记录的「抵扣后余额」与重算值一致。"""
    balances = dict(opening)
    for event in sorted(events, key=lambda row: row["seq"]):
        account = event["account"]
        balances[account] = balances.get(account, 0) + int(event["amount_cents"])
        if balances[account] != int(event["balance_after"]):
            return False
    return True


def replay(invoices: list[dict], events: list[dict],
           opening: dict[str, int]) -> dict:
    """按 (期间起, 账单号) 回放本批分录：每账单的期初/抵扣/开单/应付与每账户合计。

    「期初结转」只数上一期间边界前已生效的余额；本期间新开单的额度挂在
    「期末结转」里，下一期间才可用——与 settle 的可用性口径一致。
    出账时与页面读取共用这一条路径，保证「账单页的结转余额」与
    「下一账期账单的期初结转」是同一套口径，可以相互核对。
    """
    by_invoice: dict[str, list[dict]] = {}
    for event in events:
        by_invoice.setdefault(event["invoice_id"], []).append(event)

    active = dict(opening)
    pending: dict[str, int] = {}
    per_invoice: dict[str, dict] = {}
    accounts: dict[str, dict] = {}

    def account_row(account: str) -> dict:
        return accounts.setdefault(account, {
            "opening": int(opening.get(account, 0)), "opened": 0,
            "applied": 0, "closing": int(opening.get(account, 0))})

    for account in opening:
        account_row(account)
    ordered = sorted(invoices, key=lambda row: (row["period_start"], row["invoice_id"]))
    for _period, group in groupby(ordered, key=lambda row: row["period_start"]):
        for invoice in group:
            account = account_of(invoice)
            invoice_id = invoice["invoice_id"]
            carry_in = active.get(account, 0)
            opened = sum(int(event["amount_cents"])
                         for event in by_invoice.get(invoice_id, [])
                         if event["kind"] == OPEN)
            applied = -sum(int(event["amount_cents"])
                           for event in by_invoice.get(invoice_id, [])
                           if event["kind"] == APPLY)
            active[account] = carry_in - applied
            pending[account] = pending.get(account, 0) + opened
            balance_out = active[account] + pending[account]
            per_invoice[invoice_id] = {
                "carry_in": carry_in,
                "applied": applied,
                "opened": opened,
                "balance_out": balance_out,
                "payable": int(invoice["total_cents"]) - applied,
            }
            row = account_row(account)
            row["opened"] += opened
            row["applied"] += applied
            row["closing"] = balance_out
        # 期间边界：本期间开单的额度下一期间才生效
        for account, amount in pending.items():
            active[account] = active.get(account, 0) + amount
        pending.clear()

    summary = {
        "opening_cents": sum(row["opening"] for row in accounts.values()),
        "opened_cents": sum(row["opened"] for row in accounts.values()),
        "applied_cents": sum(row["applied"] for row in accounts.values()),
        "closing_cents": sum(row["closing"] for row in accounts.values()),
        "events": len(events),
        "chain_ok": check_balance_chain(events, opening),
        "payable_ok": all(cols["payable"] >= 0 for cols in per_invoice.values()),
    }
    return {"per_invoice": per_invoice, "accounts": accounts, "summary": summary}
