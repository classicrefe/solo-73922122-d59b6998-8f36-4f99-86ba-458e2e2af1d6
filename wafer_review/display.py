"""页面展示用：把工序点按某个变换换算到基准坐标系，生成网格。"""
from __future__ import annotations

from .geometry import Transform, apply_transform, transform_label
from .gridview import Grid, build_grid
from .model import Defect, Locator, Step

__all__ = ["transform_step", "mapped_grid", "transform_label"]


def transform_step(step: Step, transform: Transform, n: int) -> Step:
    locators = [
        Locator(loc.id, loc.pid, *apply_transform(transform, loc.row, loc.col, n))
        for loc in step.locators
    ]
    defects = [
        Defect(d.id, d.did, *apply_transform(transform, d.row, d.col, n))
        for d in step.defects
    ]
    return Step(step.id, step.seq, step.name, locators, defects)


def mapped_grid(step: Step, transform: Transform, n: int) -> Grid:
    return build_grid(transform_step(step, transform, n), n)
