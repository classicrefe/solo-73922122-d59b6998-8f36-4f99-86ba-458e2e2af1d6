"""把工序上的点排成可直接渲染的网格。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Step


@dataclass
class CellMark:
    kind: str  # locator | defect | overlay
    label: str
    muted: bool = False


@dataclass
class Grid:
    n: int
    rows: list[list[list[CellMark]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.rows:
            self.rows = [[[] for _ in range(self.n)] for _ in range(self.n)]


def build_grid(step: Step, n: int) -> Grid:
    grid = Grid(n)
    for loc in step.locators:
        grid.rows[loc.row - 1][loc.col - 1].append(CellMark("locator", loc.pid))
    for defect in step.defects:
        grid.rows[defect.row - 1][defect.col - 1].append(CellMark("defect", defect.did))
    return grid
