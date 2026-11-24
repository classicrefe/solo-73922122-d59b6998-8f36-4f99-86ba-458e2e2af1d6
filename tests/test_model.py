"""方案枚举与坐标查询的单元测试。"""
from __future__ import annotations

import pytest

from wafer_review.geometry import ALL_TRANSFORMS, IDENTITY, apply_transform, invert_transform
from wafer_review.model import (
    Defect,
    Locator,
    Step,
    Wafer,
    compute_feasibility,
    query_defect,
    step_defect_results,
)

N = 8
BASELINE_CELLS = {"L1": (2, 7), "L2": (2, 6), "L3": (7, 2), "L4": (6, 6)}


def make_wafer(transforms, baseline_locators=None, extra=None, defects=None, grid=8):
    """按给定变换构造“输入坐标”已经逆变换回去的多工序晶圆。"""
    baseline_locators = baseline_locators or {"L1": (2, 7), "L2": (2, 6)}
    defects = defects or {"D1": (4, 4)}
    steps = [
        Step(
            1,
            1,
            "光刻",
            [Locator(i, pid, *cell) for i, (pid, cell) in enumerate(baseline_locators.items(), 1)],
            [Defect(100 + i, did, *cell) for i, (did, cell) in enumerate(defects.items(), 1)],
        )
    ]
    for idx, transform in enumerate(transforms, start=2):
        locs = [
            Locator(200 + idx * 10 + i, pid, *invert_transform(transform, *cell, grid))
            for i, (pid, cell) in enumerate(baseline_locators.items(), 1)
        ]
        if extra and (idx - 2) in extra:
            for pid, cell in extra[idx - 2].items():
                locs.append(Locator(300 + idx * 10 + len(locs), pid, *cell))
        step_defects = [
            Defect(400 + idx * 10 + i, did, *invert_transform(transform, *cell, grid))
            for i, (did, cell) in enumerate(defects.items(), 1)
        ]
        steps.append(Step(idx, idx, f"工序{idx}", locs, step_defects))
    return Wafer(1, "测试片", grid, 0, "", "", steps)


def test_single_baseline_is_always_solvable():
    wafer = make_wafer([])
    feas = compute_feasibility(wafer)
    assert feas.solvable
    assert feas.scheme_count == 1
    assert feas.steps[0].transforms == (IDENTITY,)


@pytest.mark.parametrize("transform", ALL_TRANSFORMS)
def test_each_transform_is_recovered(transform):
    wafer = make_wafer([transform])
    feas = compute_feasibility(wafer)
    assert feas.solvable
    assert feas.steps[1].transforms == (transform,)
    result = step_defect_results(wafer, wafer.steps[1].id)[0]
    assert result.status == "determined"
    assert result.coordinate == (4, 4)


def test_baseline_defect_needs_no_transform():
    wafer = make_wafer([], defects={"D1": (4, 4)})
    result = step_defect_results(wafer, 1)[0]
    assert result.status == "determined"
    assert result.coordinate == (4, 4)


def test_ambiguous_symmetry_marks_pending():
    # 只用 L1：该格在“原样 / 镜像+顺时针270°”两个变换下不动，方案不唯一
    wafer = make_wafer([(True, 3)], baseline_locators={"L1": (2, 7)}, defects={"D1": (4, 4)})
    feas = compute_feasibility(wafer)
    assert feas.solvable
    transforms = feas.steps[1].transforms
    assert (False, 0) in transforms and (True, 3) in transforms
    result = step_defect_results(wafer, wafer.steps[1].id)[0]
    assert result.status == "pending"
    assert result.coordinate is None
    # 缺陷经不同可行变换会落到不同格（输入点由 (True,3) 逆变换得到）
    inputs = result.source_row, result.source_col
    images = {apply_transform(t, *inputs, N) for t in transforms}
    assert len(images) > 1
    assert set(result.candidates) == images


def test_two_locators_pin_a_unique_transform():
    wafer = make_wafer([(True, 3)], baseline_locators=BASELINE_CELLS, defects={"D1": (4, 4)})
    feas = compute_feasibility(wafer)
    assert feas.steps[1].transforms == ((True, 3),)
    assert feas.scheme_count == 1
    result = step_defect_results(wafer, wafer.steps[1].id)[0]
    assert result.status == "determined"
    assert result.coordinate == (4, 4)


def test_shared_locator_without_baseline_counterpart_couples_steps():
    """工序 2 与工序 3 共享 C1，而基准图没有 C1；工序 2 的变换固定后，工序 3 被 C1 牵制。"""
    steps = [
        Step(1, 1, "基准", [Locator(1, "L1", 2, 7), Locator(2, "L2", 2, 6)], []),
        Step(
            2,
            2,
            "工序2",
            [Locator(3, "L1", 2, 7), Locator(4, "L2", 2, 6), Locator(5, "C1", 4, 5)],
            [Defect(6, "D1", 3, 3)],
        ),
        # 若没有 C1，工序 3 的 8 种变换都可行；C1 把它压到 2 种。
        Step(3, 3, "工序3", [Locator(7, "C1", 4, 5)], [Defect(8, "D1", 3, 3)]),
    ]
    wafer = Wafer(1, "耦合", 8, 0, "", "", steps)
    feas = compute_feasibility(wafer)
    assert feas.solvable
    assert feas.scheme_count == 2
    assert feas.steps[0].transforms == (IDENTITY,)
    assert feas.steps[1].transforms == (IDENTITY,)
    assert len(feas.steps[2].transforms) == 2
    assert feas.steps[2].unanchored_locators == ("C1",)

    result = step_defect_results(wafer, 3)[0]
    assert result.status == "pending"
    assert len(result.candidates) == 2

    # 撤去工序 2 的 C1 之后，工序 3 失去跨工序约束，8 种变换全部可行。
    wafer.steps[1].locators = [loc for loc in wafer.steps[1].locators if loc.pid != "C1"]
    feas2 = compute_feasibility(wafer)
    assert feas2.scheme_count == 8
    assert len(feas2.steps[2].transforms) == 8


def test_no_solution_when_constraints_conflict():
    steps = [
        Step(1, 1, "基准", [Locator(1, "L2", 2, 6), Locator(2, "L1", 1, 1)], []),
        Step(2, 2, "工序2", [Locator(3, "L2", 2, 6), Locator(4, "L1", 2, 7)], [Defect(5, "D1", 3, 3)]),
    ]
    wafer = Wafer(1, "冲突", 8, 0, "", "", steps)
    feas = compute_feasibility(wafer)
    assert not feas.solvable
    assert feas.scheme_count == 0
    assert feas.steps[1].transforms == ()
    result = step_defect_results(wafer, 2)[0]
    assert result.status == "no_solution"
    assert result.coordinate is None


def test_cross_step_conflict_is_detected():
    """两道没有基准锚点的工序通过两个同号点互相约束；两点间距不一致则无解。"""
    steps = [
        Step(1, 1, "基准", [], []),
        Step(
            2,
            2,
            "工序2",
            [Locator(1, "C1", 4, 5), Locator(2, "C2", 2, 2)],
            [Defect(3, "D1", 3, 3)],
        ),
        Step(
            3,
            3,
            "工序3",
            [Locator(4, "C1", 4, 5), Locator(5, "C2", 2, 3)],
            [Defect(6, "D1", 3, 3)],
        ),
    ]
    wafer = Wafer(1, "冲突", 8, 0, "", "", steps)
    assert not compute_feasibility(wafer).solvable
    # 把工序 3 的 C2 挪到与工序 2 一致的位置后恢复可解
    wafer.steps[2].locators[1].row, wafer.steps[2].locators[1].col = 2, 2
    feas = compute_feasibility(wafer)
    assert feas.solvable
    assert feas.scheme_count == 8


def test_query_unknown_defect_raises():
    from wafer_review.model import ValidationError

    wafer = make_wafer([], defects={"D1": (1, 1)})
    with pytest.raises(ValidationError):
        query_defect(wafer, 1, "NOPE")


def test_transform_label_set_stable_under_relabelling():
    wafer = make_wafer([(False, 2)])
    feasibility = compute_feasibility(wafer)
    assert feasibility.steps[1].labels == ("旋转180°",)
