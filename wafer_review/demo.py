"""演示数据：6 道工序、8×8 晶粒网格。

六张图之间故意安排了多种对齐关系（旋转、镜像、镜像+旋转），并且让“封装”工序
只有 C1 一个定位点、且基准图没有 C1——它必须靠与“沉积”共享的 C1 才能对齐。
因此：

* 封装 D101/D103/D105 能给出唯一坐标；
* 封装 D102/D104 在可行方案下会落到不同格子，标为待定；
* 撤去“沉积”或“封装”的 C1，封装的查询会全部变成待定；
* 把封装的 C1 更正到 (4,4)，D104 会从待定变成确定 (5,5)。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .geometry import Transform, invert_transform

GRID = 8

BASELINE_LOCATORS: dict[str, tuple[int, int]] = {
    "L1": (2, 7),
    "L2": (2, 6),
    "L3": (7, 2),
    "L4": (6, 6),
    "X1": (4, 4),
}

BASELINE_DEFECTS: dict[str, tuple[int, int]] = {
    "D101": (4, 4),
    "D102": (5, 3),
    "D103": (8, 8),
    "D104": (4, 5),
    "D105": (1, 1),
}


@dataclass(frozen=True)
class DemoStep:
    name: str
    transform: Transform
    locators: tuple[str, ...]
    defects: tuple[str, ...]
    extra_locators: tuple[tuple[str, tuple[int, int]], ...] = ()


# C1 是跨工序定位点（基准图没有它）：沉积与封装都把它放在 (4,5)。
DEMO_STEPS: tuple[DemoStep, ...] = (
    DemoStep("光刻", (False, 0), ("L1", "L2", "L3", "L4", "X1"), tuple(BASELINE_DEFECTS)),
    DemoStep("刻蚀", (False, 1), ("L1", "L2", "L3", "L4"), tuple(BASELINE_DEFECTS)),
    DemoStep(
        "沉积",
        (True, 0),
        ("L1", "L2", "L3", "L4", "X1"),
        tuple(BASELINE_DEFECTS),
        (("C1", (4, 5)),),
    ),
    DemoStep("研磨", (False, 2), ("L1", "L2", "L3", "L4"), tuple(BASELINE_DEFECTS)),
    DemoStep("检测", (True, 1), ("L1", "L2", "L3", "L4"), tuple(BASELINE_DEFECTS)),
    DemoStep("封装", (False, 3), (), tuple(BASELINE_DEFECTS), (("C1", (4, 5)),)),
)


def render_csv(step: DemoStep) -> str:
    """按工序的变换把基准坐标逆变换成该道工序图上的输入坐标。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["类型", "编号", "行", "列"])
    for pid in step.locators:
        row, col = invert_transform(step.transform, *BASELINE_LOCATORS[pid], GRID)
        writer.writerow(["定位点", pid, row, col])
    for pid, cell in step.extra_locators:
        writer.writerow(["定位点", pid, *cell])
    for did in step.defects:
        row, col = invert_transform(step.transform, *BASELINE_DEFECTS[did], GRID)
        writer.writerow(["缺陷", did, row, col])
    return buf.getvalue()


def demo_samples() -> list[tuple[str, str]]:
    """返回 [(工序名, CSV 文本), ...]，第一个为基准。"""
    return [(step.name, render_csv(step)) for step in DEMO_STEPS]


def expected_baseline() -> dict[str, tuple[int, int]]:
    return dict(BASELINE_DEFECTS)


def transform_of(name: str) -> Transform:
    return next(s.transform for s in DEMO_STEPS if s.name == name)
