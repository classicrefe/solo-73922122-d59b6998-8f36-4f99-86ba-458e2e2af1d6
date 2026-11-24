"""在临时 SQLite 上测试服务层与事务语义。"""
from __future__ import annotations

import pytest

from wafer_review.db import connect, init_db
from wafer_review.model import (
    NoSolutionError,
    ValidationError,
    analyze,
)
from wafer_review.service import (
    MAX_STEPS,
    StepInput,
    add_locator,
    correct_locator,
    create_wafer,
    delete_wafer,
    list_wafers,
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
    path = tmp_path / "test.sqlite3"
    init_db(path)
    return path


def sample_steps():
    return [
        StepInput(name, open(path, encoding="utf-8").read()) for name, path in SAMPLES
    ]


def load(db):
    with connect(db) as conn:
        return load_wafer(conn, 1)


def test_create_and_query(db):
    wafer_id = create_wafer(db, "LOT-1", 8, sample_steps())
    assert wafer_id == 1
    wafer = load(db)
    assert [s.name for s in wafer.steps] == [n for n, _ in SAMPLES]
    analysis = analyze(wafer)
    assert analysis.solvable
    assert analysis.feasibility.scheme_count == 2

    # 基准工序直接给出自身坐标
    assert analysis.query(wafer.steps[0], "D101").coordinate == (4, 4)
    # 其余工序均已对齐
    for step in wafer.steps[1:5]:
        assert analysis.query(step, "D101").coordinate == (4, 4)
    # 封装只有 C1 一个非基准定位点，D102/D104 待定
    assert analysis.query(wafer.steps[5], "D102").status == "pending"
    assert analysis.query(wafer.steps[5], "D104").status == "pending"


def test_create_rejects_bad_csv(db):
    steps = [StepInput("光刻", "类型,编号,行,列\n定位点,L1,2,7\n")]
    with pytest.raises(ValidationError):
        create_wafer(db, "X", 8, [StepInput("a", ""), StepInput("b", "")])
    with pytest.raises(ValidationError):
        create_wafer(db, "X", 8, steps + [StepInput("坏", "foo,bar\n")])
    with pytest.raises(ValidationError):
        create_wafer(db, "X", 8, [StepInput("a", "类型,编号,行,列\n定位点,L1,9,1\n")])
    with connect(db) as conn:
        assert list_wafers(conn) == []


def test_create_rejects_more_than_six_steps(db):
    steps = [StepInput(f"s{i}", "类型,编号,行,列\n缺陷,D1,1,1\n") for i in range(MAX_STEPS + 1)]
    with pytest.raises(ValidationError):
        create_wafer(db, "X", 8, steps)


def test_upload_with_no_solution_is_not_saved(db):
    good = "类型,编号,行,列\n定位点,L1,1,1\n缺陷,D1,2,2\n"
    bad = "类型,编号,行,列\n定位点,L2,3,3\n"
    # 基准的 L1 在 (1,1)，第二张图的 L1 需要能对到 (1,1)，但 L2 不存在于基准，不冲突；
    # 这里构造真正的无解：基准两个点，第二图同号点间距无法由任何 D4 变换保持。
    base = "类型,编号,行,列\n定位点,A,1,1\n定位点,B,1,2\n缺陷,D1,2,2\n"
    bad2 = "类型,编号,行,列\n定位点,A,1,1\n定位点,B,3,3\n"
    assert good and bad
    with pytest.raises(NoSolutionError):
        create_wafer(db, "X", 8, [StepInput("基准", base), StepInput("工艺", bad2)])
    with connect(db) as conn:
        assert list_wafers(conn) == []


def test_correct_locator_updates_other_steps(db):
    create_wafer(db, "LOT-1", 8, sample_steps())
    wafer = load(db)
    # 封装 C1 的错误坐标会导致无解，操作应被拒绝，原定位点保留
    c1 = wafer.steps[5].locators[0]
    original = (c1.row, c1.col)
    with pytest.raises(NoSolutionError):
        correct_locator(db, 1, c1.id, 1, 1)
    assert (load(db).steps[5].locators[0].row, load(db).steps[5].locators[0].col) == original
    assert load(db).revision == 0

    # 改到 (4,4) 仍可解，且封装的查询结果随之变化：D104 从待定变确定
    before = analyze(load(db)).query(load(db).steps[5], "D104")
    assert before.status == "pending"
    correct_locator(db, 1, c1.id, 4, 4)
    wafer = load(db)
    assert wafer.revision == 1
    after = analyze(wafer).query(wafer.steps[5], "D104")
    assert after.status == "determined"
    assert after.coordinate == (5, 5)


def test_remove_locator_updates_all_steps(db):
    create_wafer(db, "LOT-1", 8, sample_steps())
    wafer = load(db)
    before = {s.name: analyze(wafer).query(s, "D101") for s in wafer.steps}
    assert before["封装"].status == "determined"

    # 撤去沉积的 C1：封装失去唯一的跨工序约束，D101 变为待定
    c1 = next(loc for loc in wafer.steps[2].locators if loc.pid == "C1")
    remove_locator(db, 1, c1.id)
    wafer = load(db)
    assert wafer.revision == 1
    analysis = analyze(wafer)
    assert analysis.feasibility.scheme_count == 8
    assert analysis.query(wafer.steps[5], "D101").status == "pending"
    # 其他工序不受影响
    assert analysis.query(wafer.steps[2], "D101").coordinate == (4, 4)


def test_remove_baseline_locator_is_allowed_when_still_solvable(db):
    create_wafer(db, "LOT-1", 8, sample_steps())
    wafer = load(db)
    l2 = next(loc for loc in wafer.steps[0].locators if loc.pid == "L2")
    remove_locator(db, 1, l2.id)
    assert len(load(db).steps[0].locators) == 4


def test_add_locator_validation(db):
    create_wafer(db, "LOT-1", 8, sample_steps())
    wafer = load(db)
    with pytest.raises(ValidationError):
        add_locator(db, 1, wafer.steps[5].id, "C1", 6, 6)  # 同工序编号重复
    with pytest.raises(ValidationError):
        add_locator(db, 1, wafer.steps[5].id, "Z9", 0, 6)  # 越界
    add_locator(db, 1, wafer.steps[5].id, "Z9", 6, 6)
    wafer = load(db)
    assert any(loc.pid == "Z9" for loc in wafer.steps[5].locators)


def test_delete_cascades(db):
    create_wafer(db, "LOT-1", 8, sample_steps())
    delete_wafer(db, 1)
    with connect(db) as conn:
        assert list_wafers(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM locator").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM defect").fetchone()[0] == 0


def test_failed_write_rolls_back_atomically(db):
    create_wafer(db, "LOT-1", 8, sample_steps())
    wafer = load(db)
    before_rows = [(l.pid, l.row, l.col) for l in wafer.steps[2].locators]
    c1 = next(loc for loc in wafer.steps[2].locators if loc.pid == "C1")
    with pytest.raises(NoSolutionError):
        correct_locator(db, 1, c1.id, 1, 1)
    after_rows = [(l.pid, l.row, l.col) for l in load(db).steps[2].locators]
    assert before_rows == after_rows
    assert load(db).revision == 0


def test_unknown_locator_id_rejected(db):
    create_wafer(db, "LOT-1", 8, sample_steps())
    with pytest.raises(ValidationError):
        remove_locator(db, 1, 99999)
    with pytest.raises(ValidationError):
        correct_locator(db, 1, 99999, 1, 1)
