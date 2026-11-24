"""领域模型与坐标查询。

约束：为每道工序选择一个 D4 变换（基准图固定为恒等变换），使得在所有工序中
出现过的同一个定位点编号，经各自变换换算到基准坐标系后落在同一个格子上。

某个编号若在基准图里也有，这个公共格就是基准图上的对应定位点；若基准图没有
该编号，则只要其余各图中同号点重合即可，公共格可以是任意一格。

由于不同工序可能共享“基准图没有的同号定位点”，各工序的变换会互相牵制，
所以这里枚举的是全局方案；某工序某缺陷的可能落点，就是它在所有可行方案下
换算结果的集合。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .geometry import (
    ALL_TRANSFORMS,
    IDENTITY,
    Transform,
    apply_transform,
    transform_labels,
)

class WaferError(Exception):
    """可直接展示给用户的业务错误。"""


class ValidationError(WaferError):
    pass


class NoSolutionError(WaferError):
    pass


@dataclass
class Locator:
    id: int
    pid: str
    row: int
    col: int


@dataclass
class Defect:
    id: int
    did: str
    row: int
    col: int


@dataclass
class Step:
    id: int
    seq: int
    name: str
    locators: list[Locator] = field(default_factory=list)
    defects: list[Defect] = field(default_factory=list)

    @property
    def is_baseline(self) -> bool:
        return self.seq == 1

    def locator_map(self) -> dict[str, tuple[int, int]]:
        return {loc.pid: (loc.row, loc.col) for loc in self.locators}


@dataclass
class Wafer:
    id: int
    name: str
    grid_size: int
    revision: int
    created_at: str
    updated_at: str
    steps: list[Step] = field(default_factory=list)

    @property
    def baseline(self) -> Step:
        return self.steps[0]

    def step_by_id(self, step_id: int) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)


@dataclass(frozen=True)
class DefectResult:
    """一次“工序 + 缺陷号”查询的结果。"""

    step_id: int
    step_name: str
    did: str
    source_row: int
    source_col: int
    status: str  # determined | pending | no_solution
    coordinate: tuple[int, int] | None
    candidates: tuple[tuple[int, int], ...]
    feasible_transforms: tuple[Transform, ...]
    scheme_count: int


@dataclass(frozen=True)
class StepFeasibility:
    step_id: int
    transforms: tuple[Transform, ...]
    labels: tuple[str, ...]
    shared_locators: tuple[str, ...]
    baseline_anchors: tuple[str, ...]
    unanchored_locators: tuple[str, ...]

    @property
    def is_ambiguous(self) -> bool:
        return len(self.transforms) > 1


@dataclass(frozen=True)
class Feasibility:
    steps: tuple[StepFeasibility, ...]
    solvable: bool
    scheme_count: int
    elapsed_ms: float = 0.0

    def for_step(self, step_id: int) -> StepFeasibility | None:
        return next((s for s in self.steps if s.step_id == step_id), None)


def _enumerate(wafer: Wafer) -> tuple[int, list[set[Transform]], list[frozenset[str]], float]:
    """枚举全部可行方案，返回方案数、逐工序变换投影、逐工序参与约束的编号、耗时。"""
    start = time.perf_counter()
    steps = wafer.steps
    if not steps:
        return 0, [], [], 0.0
    n = wafer.grid_size
    # 编号 -> [(工序下标, (row, col)), ...]
    points: dict[str, list[tuple[int, tuple[int, int]]]] = {}
    for idx, step in enumerate(steps):
        for loc in step.locators:
            points.setdefault(loc.pid, []).append((idx, (loc.row, loc.col)))

    locator_maps = [s.locator_map() for s in steps]
    # 每一对工序之间共享的编号，提前算好，避免枚举时反复取交集。
    shared: dict[tuple[int, int], list[str]] = {}
    for pid, entries in points.items():
        idxs = [i for i, _ in entries]
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                shared.setdefault((i, j), []).append(pid)
                shared.setdefault((j, i), []).append(pid)

    # 只被一道工序使用的编号不产生任何约束；记下真正参与约束的编号用于页面提示。
    involved: list[set[str]] = [set() for _ in steps]
    for pid, entries in points.items():
        if len(entries) > 1:
            for i, _ in entries:
                involved[i].add(pid)

    projections: list[set[Transform]] = [set() for _ in steps]
    chosen: list[Transform] = []
    scheme_count = 0

    def consistent(i: int, transform: Transform) -> bool:
        for j in range(i):
            for pid in shared.get((i, j), ()):
                a = locator_maps[i][pid]
                b = locator_maps[j][pid]
                if apply_transform(transform, a[0], a[1], n) != apply_transform(
                    chosen[j], b[0], b[1], n
                ):
                    return False
        return True

    def walk(i: int) -> None:
        nonlocal scheme_count
        if i == len(steps):
            scheme_count += 1
            for k, t in enumerate(chosen):
                projections[k].add(t)
            return
        candidates = (IDENTITY,) if i == 0 else ALL_TRANSFORMS
        for transform in candidates:
            if consistent(i, transform):
                chosen.append(transform)
                walk(i + 1)
                chosen.pop()

    walk(0)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return scheme_count, projections, [frozenset(s) for s in involved], elapsed_ms


def _build_feasibility(
    wafer: Wafer,
    scheme_count: int,
    projections: list[set[Transform]],
    involved: list[frozenset[str]],
    elapsed_ms: float,
) -> Feasibility:
    base_ids = set(wafer.baseline.locator_map())
    step_feas: list[StepFeasibility] = []
    for idx, step in enumerate(wafer.steps):
        own = set(step.locator_map())
        constrained = own & involved[idx]
        transforms = tuple(t for t in ALL_TRANSFORMS if t in projections[idx])
        step_feas.append(
            StepFeasibility(
                step_id=step.id,
                transforms=transforms,
                labels=tuple(transform_labels(transforms)),
                shared_locators=tuple(sorted(constrained)),
                baseline_anchors=tuple(sorted(constrained & base_ids)),
                unanchored_locators=tuple(sorted(constrained - base_ids)),
            )
        )
    solvable = scheme_count > 0 and all(s.transforms for s in step_feas)
    return Feasibility(
        steps=tuple(step_feas),
        solvable=solvable,
        scheme_count=scheme_count,
        elapsed_ms=elapsed_ms,
    )


def compute_feasibility(wafer: Wafer) -> Feasibility:
    scheme_count, projections, involved, elapsed_ms = _enumerate(wafer)
    return _build_feasibility(wafer, scheme_count, projections, involved, elapsed_ms)


class Analysis:
    """在一次请求内复用枚举结果，避免为每个缺陷重复搜索。"""

    def __init__(self, wafer: Wafer) -> None:
        self.wafer = wafer
        scheme_count, projections, involved, elapsed_ms = _enumerate(wafer)
        self.feasibility = _build_feasibility(
            wafer, scheme_count, projections, involved, elapsed_ms
        )
        self._transforms = {s.step_id: s.transforms for s in self.feasibility.steps}

    @property
    def solvable(self) -> bool:
        return self.feasibility.solvable

    def transforms(self, step: Step) -> tuple[Transform, ...]:
        return self._transforms.get(step.id, ())

    def result(self, step: Step, defect: Defect) -> DefectResult:
        transforms = self.transforms(step)
        if not self.feasibility.solvable or not transforms:
            return DefectResult(
                step_id=step.id,
                step_name=step.name,
                did=defect.did,
                source_row=defect.row,
                source_col=defect.col,
                status="no_solution",
                coordinate=None,
                candidates=(),
                feasible_transforms=(),
                scheme_count=0,
            )
        if step.is_baseline:
            candidates = {(defect.row, defect.col)}
        else:
            candidates = {
                apply_transform(t, defect.row, defect.col, self.wafer.grid_size)
                for t in transforms
            }
        ordered = tuple(sorted(candidates))
        determined = len(ordered) == 1
        return DefectResult(
            step_id=step.id,
            step_name=step.name,
            did=defect.did,
            source_row=defect.row,
            source_col=defect.col,
            status="determined" if determined else "pending",
            coordinate=ordered[0] if determined else None,
            candidates=ordered,
            feasible_transforms=transforms,
            scheme_count=self.feasibility.scheme_count,
        )

    def step_results(self, step: Step) -> list[DefectResult]:
        return [self.result(step, d) for d in step.defects]

    def query(self, step: Step, did: str) -> DefectResult | None:
        defect = next((d for d in step.defects if d.did == did), None)
        return self.result(step, defect) if defect is not None else None


def analyze(wafer: Wafer) -> Analysis:
    return Analysis(wafer)


def query_defect(wafer: Wafer, step_id: int, did: str) -> DefectResult:
    step = wafer.step_by_id(step_id)
    if step is None:
        raise ValidationError("工序不存在")
    result = analyze(wafer).query(step, did)
    if result is None:
        raise ValidationError(f"工序“{step.name}”中没有缺陷 {did}")
    return result


def step_defect_results(wafer: Wafer, step_id: int) -> list[DefectResult]:
    step = wafer.step_by_id(step_id)
    if step is None:
        raise ValidationError("工序不存在")
    return analyze(wafer).step_results(step)
