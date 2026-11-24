"""跨工序缺陷“同格”判定的单元测试。

同格的口径是：在**每套全局可行方案**下，两条缺陷换算到基准坐标系后都落在
同一格；落在哪一格可以随方案变化。
"""
from __future__ import annotations

import pytest

from wafer_review.geometry import invert_transform
from wafer_review.model import (
    DIFFERENT_CELL,
    NO_SOLUTION,
    SAME_CELL,
    Defect,
    Locator,
    PairRef,
    Step,
    Wafer,
    analyze,
    evaluate_pair,
    evaluate_pairs,
)


def _aligned_steps(transforms, defects, grid=8, baseline_locators=None):
    """与 test_model.make_wafer 同款构造：各工序按给定变换对齐到基准。"""
    baseline_locators = baseline_locators or {"L1": (2, 7), "L2": (2, 6)}
    steps = [
        Step(
            1,
            1,
            "基准",
            [Locator(i, pid, *cell) for i, (pid, cell) in enumerate(baseline_locators.items(), 1)],
            [Defect(100 + i, did, *cell) for i, (did, cell) in enumerate(defects.items(), 1)],
        )
    ]
    for idx, transform in enumerate(transforms, start=2):
        locs = [
            Locator(200 + idx * 10 + i, pid, *invert_transform(transform, *cell, grid))
            for i, (pid, cell) in enumerate(baseline_locators.items(), 1)
        ]
        step_defects = [
            Defect(400 + idx * 10 + i, did, *invert_transform(transform, *cell, grid))
            for i, (did, cell) in enumerate(defects.items(), 1)
        ]
        steps.append(Step(idx, idx, f"工序{idx}", locs, step_defects))
    return Wafer(1, "同格片", grid, 0, "", "", steps)


def test_same_defect_id_aligned_steps_is_same_cell_in_every_scheme():
    # 两道工序都含 D1，且都对齐到基准 (4,4)：每套方案下两缺陷同格。
    wafer = _aligned_steps([(False, 1), (True, 0)], {"D1": (4, 4)})
    verdict = evaluate_pair(wafer, PairRef(1, "D1", 2, "D1"))
    assert verdict.status == SAME_CELL
    assert verdict.common_cell == (4, 4)
    assert verdict.scheme_count == 1  # 两个定位点钉死了唯一变换


def test_pending_defect_can_still_be_same_cell_with_another_pending_defect():
    """两条各自“待定”的缺陷，仍可能在每套方案下都同格（落点一起随方案变化）。"""
    # 两道工序共享两个同行相邻定位点（基准图都没有）：两工序的可行变换被这两个
    # 点耦合，缺陷输入坐标相同 -> 每套方案都同格；但可行变换有 8 种，缺陷的落点
    # 随方案变化，所以单独查询是待定，同格判定也不记录固定公共格。
    grid = 8
    steps = [
        Step(1, 1, "基准", [], []),
        Step(
            2,
            2,
            "工序2",
            [Locator(1, "C1", 4, 4), Locator(2, "C2", 4, 5)],
            [Defect(3, "D1", 2, 3)],
        ),
        Step(
            3,
            3,
            "工序3",
            [Locator(4, "C1", 4, 4), Locator(5, "C2", 4, 5)],
            [Defect(6, "D1", 2, 3)],
        ),
    ]
    wafer = Wafer(1, "耦合", grid, 0, "", "", steps)
    assert analyze(wafer).feasibility.scheme_count == 8
    single = analyze(wafer).query(steps[1], "D1")
    assert single.status == "pending"
    verdict = evaluate_pair(wafer, PairRef(1, "D1", 2, "D1"))
    assert verdict.status == SAME_CELL
    # 落点随方案变化 -> 不记录固定公共格
    assert verdict.common_cell is None
    assert verdict.scheme_count == 8


def test_different_cell_when_any_scheme_separates_the_defects():
    # 演示数据同款：封装 D102 在两套方案下落点不同，与基准 D101 不保证同格。
    wafer = _aligned_steps([(False, 1)], {"D1": (4, 4), "D2": (5, 3)})
    verdict = evaluate_pair(wafer, PairRef(0, "D1", 1, "D2"))
    assert verdict.status == DIFFERENT_CELL


def test_no_solution_verdict():
    steps = [
        Step(1, 1, "基准", [Locator(1, "L1", 1, 1), Locator(2, "L2", 1, 2)], [Defect(9, "D1", 2, 2)]),
        Step(2, 2, "工序2", [Locator(3, "L1", 1, 1), Locator(4, "L2", 3, 3)], [Defect(8, "D1", 3, 3)]),
    ]
    wafer = Wafer(1, "冲突", 8, 0, "", "", steps)
    verdict = evaluate_pair(wafer, PairRef(0, "D1", 1, "D1"))
    assert verdict.status == NO_SOLUTION
    assert verdict.scheme_count == 0


def test_baseline_step_uses_identity_in_every_scheme():
    wafer = _aligned_steps([(False, 2)], {"D1": (1, 1)})
    verdict = evaluate_pair(wafer, PairRef(0, "D1", 1, "D1"))
    assert verdict.status == SAME_CELL
    assert verdict.common_cell == (1, 1)


def test_evaluate_pairs_batch_matches_single_calls():
    wafer = _aligned_steps(
        [(False, 1), (True, 0), (False, 2)],
        {"D1": (4, 4), "D2": (5, 3)},
    )
    refs = [
        PairRef(1, "D1", 2, "D1"),  # 同格
        PairRef(0, "D1", 1, "D2"),  # 不同格
        PairRef(2, "D1", 3, "D1"),  # 同格
    ]
    batch = evaluate_pairs(wafer, refs)
    singles = [evaluate_pair(wafer, ref) for ref in refs]
    assert [b.status for b in batch] == [s.status for s in singles]
    assert all(b.scheme_count == s.scheme_count for b, s in zip(batch, singles))
    assert [b.status for b in batch] == [SAME_CELL, DIFFERENT_CELL, SAME_CELL]


def test_swap_order_does_not_change_verdict():
    wafer = _aligned_steps([(False, 1)], {"D1": (4, 4), "D2": (5, 3)})
    forward = evaluate_pair(wafer, PairRef(0, "D1", 1, "D2"))
    backward = evaluate_pair(wafer, PairRef(1, "D2", 0, "D1"))
    assert forward.status == backward.status


def test_unknown_step_or_defect_raises():
    from wafer_review.model import ValidationError

    wafer = _aligned_steps([(False, 1)], {"D1": (4, 4)})
    with pytest.raises(ValidationError):
        evaluate_pair(wafer, PairRef(0, "D1", 9, "D1"))
    with pytest.raises(ValidationError):
        evaluate_pair(wafer, PairRef(0, "NOPE", 1, "D1"))
    with pytest.raises(ValidationError):
        evaluate_pairs(wafer, [PairRef(0, "D1", 1, "MISSING")])
