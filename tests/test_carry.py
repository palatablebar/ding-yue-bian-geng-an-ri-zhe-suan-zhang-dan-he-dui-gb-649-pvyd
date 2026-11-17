"""超额抵扣结转：开单口径、FIFO 抵扣、应付非负、幂等重跑、失败回滚、批次替代。

手算基准（2026-02 期间 28 天，2026-03 期间 31 天）：
- SUB-2001 2400/28×4 = 342.857142… → 34286 分；预缴 240000 − 34286 = 结转 205714
- SUB-2003 1200/31×15 + 2400/31×16 = 1819.354838… → 181935 分，余额全抵，应付 0
- SUB-2004 600.00 = 60000 分，抵剩余 23779，应付 36221，余额归零
"""
from __future__ import annotations

import pytest

from app.modules.m1_ingest import service as ingest_service
from app.modules.m6_invoices import carry, reconcile
from app.modules.m6_invoices import service as invoice_service

CANCEL_SUBS = "subscriptions_cancel.csv"
CANCEL_CHANGES = "changes_cancel.csv"
NEXT_SUBS = "subscriptions_cancel_next.csv"
NEXT_CHANGES = "changes_cancel_next.csv"

OPENED = 205714
SUB2003_TOTAL = 181935
REMAINDER = OPENED - SUB2003_TOTAL  # 23779
SUB2004_PAYABLE = 60000 - REMAINDER  # 36221


def _inv(invoice_id: str, account: str, period_start: str, total: int,
         sub_id: str = "S-1") -> dict:
    return {"invoice_id": invoice_id, "account": account, "sub_id": sub_id,
            "period_start": period_start, "total_cents": total}


def _base(invoice_id: str, cents: int) -> dict:
    return {"invoice_id": invoice_id, "kind": "charge", "source": "base",
            "cents": cents}


def _settle(invoices, bases, cancels, opening=None):
    return carry.settle(invoices, bases, cancels, opening or {})


# ---------- 纯函数：开单与抵扣 ----------

def test_cancel_opens_unused_credit():
    invoices = [_inv("INV-A", "acct", "2026-02-01", 34286)]
    result = _settle(invoices, [_base("INV-A", 240000)], {"INV-A"})
    assert [event["kind"] for event in result["events"]] == ["open"]
    event = result["events"][0]
    assert event["amount_cents"] == OPENED
    assert event["balance_after"] == OPENED
    assert event["source_invoice"] == "INV-A"
    assert event["event_id"] == "CF-OPEN-INV-A"
    assert result["balances"] == {"acct": OPENED}


def test_no_cancel_no_events():
    invoices = [_inv("INV-A", "acct", "2026-02-01", 60000)]
    result = _settle(invoices, [_base("INV-A", 60000)], set())
    assert result["events"] == []
    assert result["balances"] == {}


def test_open_clamped_when_usage_exceeds_prepaid():
    # 先升配后取消：实际用量超过整期预缴，没有可结转的未用额度
    invoices = [_inv("INV-A", "acct", "2026-02-01", 167143)]
    result = _settle(invoices, [_base("INV-A", 120000)], {"INV-A"})
    assert result["events"] == []
    assert result["balances"] == {}


def test_same_period_open_not_applied():
    # 同一期间内开单的额度不能抵本期间的其他账单，只给后续期间用
    invoices = [_inv("INV-A", "acct", "2026-02-01", 34286),
                _inv("INV-B", "acct", "2026-02-01", 60000)]
    result = _settle(invoices, [_base("INV-A", 240000), _base("INV-B", 60000)],
                     {"INV-A"})
    kinds = [event["kind"] for event in result["events"]]
    assert kinds == ["open"]
    assert result["balances"] == {"acct": OPENED}


def test_next_period_applies_fifo_with_source():
    opening = {"acct": [{"source_invoice": "INV-P1", "period_start": "2026-01-01",
                         "remaining": 10000},
                        {"source_invoice": "INV-P2", "period_start": "2026-02-01",
                         "remaining": 5000}]}
    invoices = [_inv("INV-C", "acct", "2026-03-01", 12000)]
    result = _settle(invoices, [_base("INV-C", 12000)], set(), opening)
    applies = [event for event in result["events"] if event["kind"] == "apply"]
    assert [(event["amount_cents"], event["source_invoice"]) for event in applies] == [
        (-10000, "INV-P1"), (-2000, "INV-P2")]
    assert [event["balance_after"] for event in applies] == [5000, 3000]
    assert result["balances"] == {"acct": 3000}


def test_apply_clamped_at_invoice_total():
    # 余额多于账单额时只抵到账单额为止：应付不为负，余额继续结转
    opening = {"acct": [{"source_invoice": "INV-P1", "period_start": "2026-02-01",
                         "remaining": 90000}]}
    invoices = [_inv("INV-C", "acct", "2026-03-01", 60000)]
    result = _settle(invoices, [_base("INV-C", 60000)], set(), opening)
    [event] = result["events"]
    assert event["amount_cents"] == -60000
    assert event["balance_after"] == 30000
    view = carry.replay(invoices, result["events"], {"acct": 90000})
    cols = view["per_invoice"]["INV-C"]
    assert cols["payable"] == 0
    assert view["summary"]["payable_ok"] is True


def test_replay_matches_settle_chain():
    opening = {"acct": [{"source_invoice": "INV-P1", "period_start": "2026-01-01",
                         "remaining": 7000}]}
    invoices = [_inv("INV-A", "acct", "2026-02-01", 34286),
                _inv("INV-B", "acct", "2026-02-01", 60000),
                _inv("INV-C", "acct", "2026-03-01", 5000)]
    bases = [_base("INV-A", 240000), _base("INV-B", 60000), _base("INV-C", 5000)]
    result = _settle(invoices, bases, {"INV-A"}, opening)
    view = carry.replay(invoices, result["events"], {"acct": 7000})
    assert view["summary"]["chain_ok"] is True
    assert view["per_invoice"]["INV-A"]["carry_in"] == 7000
    assert view["per_invoice"]["INV-B"]["carry_in"] == 0  # 同期开单不可用
    assert view["per_invoice"]["INV-C"]["opened"] == 0
    assert view["accounts"]["acct"]["closing"] == result["balances"]["acct"]


def test_balance_chain_detects_tampering():
    events = [{"seq": 0, "account": "acct", "kind": "open", "amount_cents": 100,
               "balance_after": 101, "invoice_id": "I", "period_start": "2026-02-01"}]
    assert carry.check_balance_chain(events, {}) is False
    events[0]["balance_after"] = 100
    assert carry.check_balance_chain(events, {}) is True


# ---------- 集成：两期采信 ----------

@pytest.fixture()
def two_periods(store, settings):
    first = ingest_service.import_sample(store, settings, CANCEL_SUBS, CANCEL_CHANGES)
    second = ingest_service.import_sample(store, settings, NEXT_SUBS, NEXT_CHANGES)
    return first, second


def test_cancel_run_opens_carry(store, settings):
    run = ingest_service.import_sample(store, settings, CANCEL_SUBS, CANCEL_CHANGES)
    run_id = run["run_id"]
    events = store.list_carry_events(run_id)
    assert [(event["kind"], event["amount_cents"]) for event in events] == [
        ("open", OPENED)]
    invoices = {row["invoice_id"]: row for row in store.list_invoices(run_id)}
    assert "R_CARRY_OPEN" in invoices["INV-202602-SUB-2001"]["flags"]
    assert "R_CARRY_OPEN" not in invoices["INV-202602-SUB-2002"]["flags"]
    totals = store.list_totals(run_id)
    assert totals["carry_opened_cents"] == str(OPENED)
    assert totals["carry_balance_cents"] == str(OPENED)
    assert totals["payable_cents"] == totals["invoice_cents"] == "94286"
    ledgers = store.list_reconciliations(run_id)
    assert [row["ledger"] for row in ledgers] == ["rows", "days", "amount",
                                                  "invoices", "carry"]
    assert all(row["status"] == "ok" for row in ledgers)


def test_same_period_invoice_not_offset(store, settings):
    run = ingest_service.import_sample(store, settings, CANCEL_SUBS, CANCEL_CHANGES)
    rows = {row["invoice_id"]: row
            for row in invoice_service.invoice_rows(store, run["run_id"])}
    other = rows["INV-202602-SUB-2002"]
    assert other["carry_applied_cents"] == 0
    assert other["payable_cents"] == 60000
    cancelled = rows["INV-202602-SUB-2001"]
    assert cancelled["balance_out_cents"] == OPENED


def test_next_period_applies_and_reconciles(two_periods, store):
    _first, second = two_periods
    run_id = second["run_id"]
    events = store.list_carry_events(run_id)
    assert [(event["kind"], event["amount_cents"], event["source_invoice"])
            for event in events] == [
        ("apply", -SUB2003_TOTAL, "INV-202602-SUB-2001"),
        ("apply", -REMAINDER, "INV-202602-SUB-2001"),
    ]
    rows = {row["invoice_id"]: row
            for row in invoice_service.invoice_rows(store, run_id)}
    sub2003 = rows["INV-202603-SUB-2003"]
    assert sub2003["carry_in_cents"] == OPENED  # 与上一批的期末余额互相核对
    assert sub2003["carry_applied_cents"] == SUB2003_TOTAL
    assert sub2003["payable_cents"] == 0
    sub2004 = rows["INV-202603-SUB-2004"]
    assert sub2004["carry_in_cents"] == REMAINDER
    assert sub2004["payable_cents"] == SUB2004_PAYABLE
    totals = store.list_totals(run_id)
    assert totals["carry_applied_cents"] == str(OPENED)
    assert totals["carry_balance_cents"] == "0"
    assert totals["payable_cents"] == str(SUB2004_PAYABLE)
    assert all(row["status"] == "ok" for row in store.list_reconciliations(run_id))


def test_carry_overview_balances(two_periods, store):
    first, second = two_periods
    overview = invoice_service.carry_overview(store, second["run_id"])
    assert overview["balances"] == [
        {"account": "星野传媒", "opening": OPENED, "opened": 0,
         "applied": OPENED, "closing": 0}]
    assert overview["summary"]["events"] == 2
    first_view = invoice_service.carry_overview(store, first["run_id"])
    assert first_view["balances"][0]["closing"] == OPENED


# ---------- 幂等：同一结算重跑 ----------

def test_rebuild_is_idempotent(two_periods, store, settings):
    _first, second = two_periods
    run_id = second["run_id"]
    before_events = store.list_carry_events(run_id)
    before_totals = store.list_totals(run_id)
    invoice_service.rebuild(store, run_id, settings)
    invoice_service.rebuild(store, run_id, settings)
    assert store.list_carry_events(run_id) == before_events
    assert store.list_totals(run_id) == before_totals
    assert len([event for event in store.list_carry_events(run_id)
                if event["kind"] == "apply"]) == 2  # 不重复结转或冲销


def test_reimport_reuses_run_without_new_events(two_periods, store, settings):
    _first, second = two_periods
    again = ingest_service.import_sample(store, settings, NEXT_SUBS, NEXT_CHANGES)
    assert again["reused"] is True
    assert again["run_id"] == second["run_id"]
    assert len(store.list_runs()) == 2
    assert len(store.list_carry_events(second["run_id"])) == 2


# ---------- 失败回滚 ----------

def test_failed_settlement_rolls_back(two_periods, store):
    _first, second = two_periods
    run_id = second["run_id"]
    before_events = store.list_carry_events(run_id)
    before_invoices = store.list_invoices(run_id)
    before_totals = store.list_totals(run_id)
    bad_reconciliations = [{"seq": 0, "ledger": "carry"}]  # 缺列，必失败
    with pytest.raises(Exception):
        store.put_invoice_settlement(run_id, before_invoices, [], bad_reconciliations,
                                     {"x": "1"})
    assert store.list_carry_events(run_id) == before_events
    assert store.list_invoices(run_id) == before_invoices
    assert store.list_totals(run_id) == before_totals


def test_running_run_events_not_counted(two_periods, store):
    first, _second = two_periods
    store.create_run({
        "run_id": "SB-FAKE-RUNNING", "batch_key": "fake", "status": "running",
        "source": "t", "rule_version": "t", "created_at": "2999-01-01T00:00:00+00:00",
    })
    store.put_carry_events("SB-FAKE-RUNNING", [{
        "seq": 0, "event_id": "CF-OPEN-FAKE", "account": "星野传媒",
        "sub_id": "SUB-2099", "invoice_id": "INV-FAKE", "period_start": "2026-04-01",
        "kind": "open", "amount_cents": 99999, "balance_after": 99999,
        "source_invoice": "INV-FAKE", "note": ""}])
    history = store.carry_opening_events(first["run_id"], set())
    assert all(event["run_id"] != "SB-FAKE-RUNNING" for event in history)


# ---------- 批次替代 ----------

def test_superseded_period_events_replaced(two_periods, store):
    """同一 (账户, 期间) 重结后，余额只数最新批次；旧批次分录仍在但不重复计入。"""
    first, second = two_periods
    store.create_run({
        "run_id": "SB-RESETTLE-03", "batch_key": "reset", "status": "done",
        "source": "t", "rule_version": "t", "created_at": "2999-01-01T00:00:00+00:00",
    })
    # 新批次重结 2026-03：只开出 100000 的抵扣（场景：修正后用量变小）
    store.put_invoice_settlement("SB-RESETTLE-03", [{
        "invoice_id": "INV-202603-SUB-2003", "sub_id": "SUB-2003", "account": "星野传媒",
        "period_start": "2026-03-01", "period_end": "2026-04-01", "period_days": 31,
        "billed_days": 31, "free_days": 0, "charge_cents": 100000, "credit_cents": 0,
        "total_cents": 100000, "exact_total": "0", "status": "ok", "flags": [],
        "source_row": 1,
    }], [{
        "seq": 0, "event_id": "CF-APPLY-INV-202603-SUB-2003-1", "account": "星野传媒",
        "sub_id": "SUB-2003", "invoice_id": "INV-202603-SUB-2003",
        "period_start": "2026-03-01", "kind": "apply", "amount_cents": -100000,
        "balance_after": OPENED - 100000, "source_invoice": "INV-202602-SUB-2001",
        "note": "",
    }], [], {})
    history = store.carry_opening_events("SB-RESETTLE-03",
                                         {("星野传媒", "2026-03-01")})
    # 期初只含 2026-02 的开单；本批覆盖的 2026-03 不算
    assert [(event["kind"], event["amount_cents"]) for event in history] == [
        ("open", OPENED)]
    # 一个更晚的批次看余额：2026-03 只数重结批次的 -100000，旧批次的 -205714 被替代
    store.create_run({
        "run_id": "SB-LATER", "batch_key": "later", "status": "running",
        "source": "t", "rule_version": "t", "created_at": "2999-01-02T00:00:00+00:00",
    })
    history = store.carry_opening_events("SB-LATER", set())
    lots = carry.lots_from_events(history)
    assert carry.balances_from_lots(lots) == {"星野传媒": OPENED - 100000}
    assert second["run_id"] != "SB-RESETTLE-03"


# ---------- 接口与导出 ----------

def test_carry_api_and_detail(client):
    client.post("/api/subscriptions/import",
                json={"subscriptions": CANCEL_SUBS, "changes": CANCEL_CHANGES})
    client.post("/api/subscriptions/import",
                json={"subscriptions": NEXT_SUBS, "changes": NEXT_CHANGES})
    data = client.get("/api/carry").json()
    assert data["balances"][0]["closing"] == 0
    assert [event["kind"] for event in data["events"]] == ["apply", "apply"]
    detail = client.get("/api/invoices/INV-202603-SUB-2003").json()
    assert detail["invoice"]["payable_cents"] == 0
    assert detail["carry_events"][0]["source_invoice"] == "INV-202602-SUB-2001"
    overview = client.get("/api/invoices").json()
    row = [item for item in overview["invoices"]
           if item["invoice_id"] == "INV-202603-SUB-2004"][0]
    assert row["payable"] == "362.21"
    assert row["carry_in"] == "237.79"


def test_carry_csv_export(client):
    client.post("/api/subscriptions/import",
                json={"subscriptions": CANCEL_SUBS, "changes": CANCEL_CHANGES})
    response = client.get("/api/export/carry.csv")
    assert response.status_code == 200
    assert response.headers["x-row-count"] == "1"
    text = response.text.lstrip("\ufeff")
    assert text.splitlines()[0].startswith("事件号,类型,账户")
    assert "CF-OPEN-INV-202602-SUB-2001" in text
    assert "2057.14" in text
    invoices = client.get("/api/export/invoices.csv")
    header = invoices.text.lstrip("\ufeff").splitlines()[0]
    assert "结转抵扣(元)" in header and "应付(元)" in header


def test_carry_ledger_flags_inconsistency():
    view = {"summary": {"opening_cents": 10, "opened_cents": 5, "applied_cents": 3,
                        "closing_cents": 12, "chain_ok": True, "payable_ok": True}}
    ledger = reconcile.carry_ledger(view)
    assert ledger["status"] == "ok" and ledger["diff_text"] == "0"
    view["summary"]["payable_ok"] = False
    assert reconcile.carry_ledger(view)["status"] == "mismatch"
    view["summary"]["payable_ok"] = True
    view["summary"]["chain_ok"] = False
    assert reconcile.carry_ledger(view)["status"] == "mismatch"
