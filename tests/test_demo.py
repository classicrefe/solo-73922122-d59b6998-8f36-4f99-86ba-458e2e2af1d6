"""演示数据自检：样本 CSV 应复现文档里声明的基准坐标。"""
from __future__ import annotations

import pytest

from wafer_review.db import connect, init_db
from wafer_review.demo import demo_samples, expected_baseline
from wafer_review.model import analyze
from wafer_review.service import StepInput, create_wafer, load_wafer


@pytest.fixture()
def wafer(tmp_path):
    path = tmp_path / "demo.sqlite3"
    init_db(path)
    steps = [StepInput(name, text) for name, text in demo_samples()]
    wafer_id = create_wafer(path, "DEMO", 8, steps)
    with connect(path) as conn:
        return load_wafer(conn, wafer_id)


def test_demo_has_six_steps_and_two_schemes(wafer):
    analysis = analyze(wafer)
    assert len(wafer.steps) == 6
    assert analysis.solvable
    assert analysis.feasibility.scheme_count == 2
    assert analysis.feasibility.for_step(wafer.steps[5].id).labels == ("顺时针270°", "左右镜像")


def test_demo_determined_and_pending(wafer):
    analysis = analyze(wafer)
    expected = expected_baseline()
    for step in wafer.steps[:5]:
        for did, cell in expected.items():
            result = analysis.query(step, did)
            assert result.status == "determined", (step.name, did)
            assert result.coordinate == cell, (step.name, did)
    packing = wafer.steps[5]
    assert analysis.query(packing, "D101").coordinate == (4, 4)
    assert analysis.query(packing, "D102").status == "pending"
    assert analysis.query(packing, "D103").coordinate == (8, 8)
    assert analysis.query(packing, "D104").status == "pending"
    assert analysis.query(packing, "D105").coordinate == (1, 1)


def test_removing_c1_makes_packing_pending(wafer):
    # 沉积的 C1 是封装的唯一跨工序约束
    wafer.steps[2].locators = [loc for loc in wafer.steps[2].locators if loc.pid != "C1"]
    after = analyze(wafer)
    assert after.feasibility.scheme_count == 8
    for did in ("D101", "D103", "D105"):
        assert after.query(wafer.steps[5], did).status == "pending"
