"""同格复查记录的持久化与事务语义测试。"""
from __future__ import annotations

import pytest

from wafer_review.db import connect, init_db
from wafer_review.model import DIFFERENT_CELL, SAME_CELL, ValidationError
from wafer_review.pairs import (
    CONFIRMED,
    PENDING,
    delete_pair,
    list_pairs,
    refresh_confirmed_pairs,
    upsert_pair,
    verdicts_for,
)
from wafer_review.service import (
    StepInput,
    add_locator,
    correct_locator,
    create_wafer,
    delete_wafer,
    load_wafer,
    remove_locator,
)

SAMPLES = [
    ("光刻", "/workspace/samples/1_光刻.csv"),
    ("刻蚀", "/workspace/samples/2_刻蚀.csv"),
    ("沉积", "/workspace/samples/3_沉积.csv"),
    ("研磨", "/workspace/samples/4_研磨.csv"),
    ("检测", "/workspace/samples/5_检测.csv"),
    ("封装", "/workspace/samples/6_封装.csv"),
]


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "pairs.sqlite3"
    init_db(path)
    steps = [StepInput(name, open(path, encoding="utf-8").read()) for name, path in SAMPLES]
    create_wafer(path, "LOT-PAIR", 8, steps)
    return path


def _wafer(db):
    with connect(db) as conn:
        return load_wafer(conn, 1)


def _submit(db, sa, da, sb, db_):
    with connect(db) as conn:
        wafer = load_wafer(conn, 1)
        conn.execute("BEGIN IMMEDIATE")
        rec, verdict = upsert_pair(conn, wafer, sa, da, sb, db_)
        conn.execute("COMMIT")
    return rec, verdict


def test_same_cell_pair_is_confirmed(db):
    rec, verdict = _submit(db, 1, "D101", 2, "D101")
    assert verdict == SAME_CELL
    assert rec.status == CONFIRMED


def test_swap_order_keeps_single_record(db):
    rec1, _ = _submit(db, 1, "D101", 2, "D101")
    rec2, _ = _submit(db, 2, "D101", 1, "D101")
    assert rec1.id == rec2.id
    with connect(db) as conn:
        assert len(list_pairs(conn, 1)) == 1
        stored = list_pairs(conn, 1)[0]
    # 规范化后工序 id 小者在前
    assert (stored.step_id_a, stored.step_id_b) == (1, 2)


def test_non_same_cell_pair_is_pending_and_kept(db):
    rec, verdict = _submit(db, 1, "D101", 6, "D102")
    assert verdict == DIFFERENT_CELL
    assert rec.status == PENDING
    with connect(db) as conn:
        assert len(list_pairs(conn, 1)) == 1


def test_validation_rejects_same_step_and_unknown_defect(db):
    with connect(db) as conn:
        wafer = load_wafer(conn, 1)
        for args in [
            (1, "D101", 1, "D102"),     # 同一道工序
            (1, "NOPE", 2, "D101"),     # 缺陷不存在
            (1, "D101", 99, "D101"),    # 工序不存在
            (1, "  ", 2, "D101"),       # 编号为空
        ]:
            with pytest.raises(ValidationError):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    upsert_pair(conn, wafer, *args)
                finally:
                    conn.execute("ROLLBACK")


def test_locator_remove_demotes_losing_confirmed_pairs_only(db):
    # 光刻 D101 ⇄ 封装 D101：依赖沉积 C1 提供的跨工序约束
    rec, _ = _submit(db, 1, "D101", 6, "D101")
    assert rec.status == CONFIRMED
    # 另一条不受影响的确认记录
    other, _ = _submit(db, 1, "D101", 2, "D101")
    assert other.status == CONFIRMED

    wafer = _wafer(db)
    c1_dep = next(loc.id for loc in wafer.steps[2].locators if loc.pid == "C1")
    demoted = remove_locator(db, 1, c1_dep)
    assert demoted == 1
    with connect(db) as conn:
        records = {r.id: r.status for r in list_pairs(conn, 1)}
    assert records[rec.id] == PENDING
    assert records[other.id] == CONFIRMED


def test_restoring_locator_does_not_auto_reconfirm(db):
    rec, _ = _submit(db, 1, "D101", 6, "D101")
    wafer = _wafer(db)
    c1_dep = next(loc.id for loc in wafer.steps[2].locators if loc.pid == "C1")
    remove_locator(db, 1, c1_dep)

    wafer = _wafer(db)
    dep_step = wafer.steps[2].id
    add_locator(db, 1, dep_step, "C1", 4, 5)
    with connect(db) as conn:
        records = list_pairs(conn, 1)
        wafer = load_wafer(conn, 1)
        live = verdicts_for(wafer, records)
    assert records[0].status == PENDING          # 仍待人工复核
    assert live[rec.id] == SAME_CELL             # 但即时判定已恢复同格

    # 人工重新确认后才回到 confirmed
    rec2, verdict = _submit(db, 1, "D101", 6, "D101")
    assert rec2.id == rec.id and verdict == SAME_CELL and rec2.status == CONFIRMED


def test_failed_correction_rolls_back_locator_and_pair_status_together(db):
    rec, _ = _submit(db, 1, "D101", 6, "D101")
    wafer = _wafer(db)
    c1_pack = next(loc.id for loc in wafer.steps[5].locators if loc.pid == "C1")
    original = next(loc for loc in wafer.steps[5].locators if loc.pid == "C1")
    from wafer_review.model import NoSolutionError

    with pytest.raises(NoSolutionError):
        correct_locator(db, 1, c1_pack, 1, 1)

    wafer_after = _wafer(db)
    kept = next(loc for loc in wafer_after.steps[5].locators if loc.pid == "C1")
    assert (kept.row, kept.col) == (original.row, original.col)
    with connect(db) as conn:
        assert next(r for r in list_pairs(conn, 1) if r.id == rec.id).status == CONFIRMED
    assert wafer_after.revision == 0


def test_refresh_only_touches_confirmed_records(db):
    # 一条待复核记录即使在当前定位点下同格，也不被 refresh 改动
    _submit(db, 1, "D101", 6, "D102")  # pending
    with connect(db) as conn:
        wafer = load_wafer(conn, 1)
        demoted = refresh_confirmed_pairs(conn, wafer)
    assert demoted == 0
    with connect(db) as conn:
        assert list_pairs(conn, 1)[0].status == PENDING


def test_delete_pair_and_wafer_cascade(db):
    rec, _ = _submit(db, 1, "D101", 2, "D101")
    with connect(db) as conn:
        delete_pair(conn, 1, rec.id)
        assert list_pairs(conn, 1) == []
    _submit(db, 1, "D101", 2, "D101")
    delete_wafer(db, 1)
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM defect_pair").fetchone()[0] == 0


def test_listing_orders_confirmed_before_pending(db):
    _submit(db, 1, "D101", 6, "D102")  # pending first
    _submit(db, 1, "D101", 2, "D101")  # confirmed second
    with connect(db) as conn:
        statuses = [r.status for r in list_pairs(conn, 1)]
    assert statuses == [CONFIRMED, PENDING]
