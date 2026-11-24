"""把各工序的定位点与缺陷换算到同一基准坐标系，用于叠加查看。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import Transform, apply_transform
from .gridview import CellMark, Grid
from .model import Analysis, Step, Wafer


@dataclass
class Layer:
    step_id: int
    name: str
    transform: Transform
    label: str
    locators: list[tuple[str, int, int]] = field(default_factory=list)
    defects: list[tuple[str, int, int]] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)


def step_layer(analysis: Analysis, step: Step) -> Layer:
    """把某工序的所有点按“第一个可行变换”换算到基准坐标。"""
    transforms = analysis.transforms(step)
    transform = transforms[0] if transforms else (False, 0)
    n = analysis.wafer.grid_size
    label = analysis.feasibility.for_step(step.id)
    return Layer(
        step_id=step.id,
        name=step.name,
        transform=transform,
        label=label.labels[0] if label and label.labels else "无可行变换",
        locators=[
            (loc.pid, *apply_transform(transform, loc.row, loc.col, n)) for loc in step.locators
        ],
        defects=[
            (d.did, *apply_transform(transform, d.row, d.col, n)) for d in step.defects
        ],
        pending=[d.did for d in step.defects if analysis.result(step, d).status == "pending"],
    )


def baseline_cells(wafer: Wafer) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
    base = wafer.baseline
    return base.locator_map(), {d.did: (d.row, d.col) for d in base.defects}


def compare_grid(analysis: Analysis) -> Grid:
    """以基准图为底，叠加其余工序换算后的缺陷位置。"""
    wafer = analysis.wafer
    n = wafer.grid_size
    grid = Grid(n)
    base_locs, base_defects = baseline_cells(wafer)
    for pid, (r, c) in base_locs.items():
        grid.rows[r - 1][c - 1].append(CellMark("locator", pid))
    for did, (r, c) in base_defects.items():
        grid.rows[r - 1][c - 1].append(CellMark("defect", did))
    for step in wafer.steps[1:]:
        layer = step_layer(analysis, step)
        for did, r, c in layer.defects:
            mark = CellMark("overlay", did, muted=did in layer.pending)
            grid.rows[r - 1][c - 1].append(mark)
    return grid
