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


# 同格判定结果：same_cell=每套可行方案都同格；different=存在不同格的方案；
# no_solution=当前定位点没有任何可行方案。
SAME_CELL = "same_cell"
DIFFERENT_CELL = "different"
NO_SOLUTION = "no_solution"


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
    """枚举全部可行方案，返回方案数、逐工序变换投影、逐工序参与约束的编号、耗时。

    可行方案是“每道工序各选一个变换”的完整组合（基准固定恒等）。这里除了统计
    方案数，还把每套方案逐工序的变换投影记下来，供逐缺陷查询使用；跨工序“同格”
    判定需要完整方案，由 :func:`iter_feasible_schemes` 另行遍历。
    """
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


def iter_feasible_schemes(wafer: Wafer):
    """逐套产出全局可行方案，每套方案是按工序顺序排列的变换元组。

    与 :func:`_enumerate` 共用同一套回溯约束，但这里不缓存完整方案（最多
    8⁵ = 32768 套），而是边枚举边交给调用方判断，避免把全部方案常驻内存。
    """
    steps = wafer.steps
    if not steps:
        return
    n = wafer.grid_size
    points: dict[str, list[tuple[int, tuple[int, int]]]] = {}
    for idx, step in enumerate(steps):
        for loc in step.locators:
            points.setdefault(loc.pid, []).append((idx, (loc.row, loc.col)))

    locator_maps = [s.locator_map() for s in steps]
    shared: dict[tuple[int, int], list[str]] = {}
    for pid, entries in points.items():
        idxs = [i for i, _ in entries]
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                shared.setdefault((i, j), []).append(pid)
                shared.setdefault((j, i), []).append(pid)

    chosen: list[Transform] = []

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

    def walk(i: int):
        if i == len(steps):
            yield tuple(chosen)
            return
        candidates = (IDENTITY,) if i == 0 else ALL_TRANSFORMS
        for transform in candidates:
            if consistent(i, transform):
                chosen.append(transform)
                yield from walk(i + 1)
                chosen.pop()

    yield from walk(0)


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


@dataclass(frozen=True)
class PairRef:
    """一对待判同格的跨工序缺陷（按工序下标引用）。"""

    step_index_a: int
    did_a: str
    step_index_b: int
    did_b: str


@dataclass(frozen=True)
class SameCellVerdict:
    """一条缺陷对在**所有**全局可行方案下的同格判定。

    ``status``：
      * ``same_cell``——每套可行方案都把两条记录落到同一格；
      * ``different``——存在让它们落到不同格的方案（即使另有方案同格也不算）；
      * ``no_solution``——当前定位点没有任何可行方案。

    ``common_cell`` 仅在 same_cell 时有值；具体落在哪一格可随方案而变，所以
    这里给出的是“当前定位点下”的公共格，复查记录不把它当作固定依据保存。
    """

    status: str
    common_cell: tuple[int, int] | None = None
    scheme_count: int = 0


def evaluate_pair(wafer: Wafer, pair: PairRef) -> SameCellVerdict:
    """判定一对缺陷在每套全局可行方案下是否都落在基准坐标系的同一格。"""
    steps = wafer.steps
    if not (0 <= pair.step_index_a < len(steps) and 0 <= pair.step_index_b < len(steps)):
        raise ValidationError("工序不存在")
    step_a, step_b = steps[pair.step_index_a], steps[pair.step_index_b]
    defect_a = next((d for d in step_a.defects if d.did == pair.did_a), None)
    defect_b = next((d for d in step_b.defects if d.did == pair.did_b), None)
    if defect_a is None:
        raise ValidationError(f"工序“{step_a.name}”中没有缺陷 {pair.did_a}")
    if defect_b is None:
        raise ValidationError(f"工序“{step_b.name}”中没有缺陷 {pair.did_b}")

    n = wafer.grid_size
    scheme_count = 0
    common_cell: tuple[int, int] | None = None
    for scheme in iter_feasible_schemes(wafer):
        scheme_count += 1
        cell_a = apply_transform(
            scheme[pair.step_index_a], defect_a.row, defect_a.col, n
        )
        cell_b = apply_transform(
            scheme[pair.step_index_b], defect_b.row, defect_b.col, n
        )
        if cell_a != cell_b:
            return SameCellVerdict(DIFFERENT_CELL, None, scheme_count)
        if common_cell is None:
            common_cell = cell_a
        elif cell_a != common_cell:
            # 每套方案都同格，但落点随方案变化——仍然满足“同格”，只是格不固定。
            common_cell = None
    if scheme_count == 0:
        return SameCellVerdict(NO_SOLUTION, None, 0)
    return SameCellVerdict(SAME_CELL, common_cell, scheme_count)


def evaluate_pairs(wafer: Wafer, pairs: list[PairRef]) -> list[SameCellVerdict]:
    """批量判定多条缺陷对，共用一次方案枚举。

    枚举在调用方只做一次：每发现一套可行方案就推进所有缺陷对的判定，任何一对
    已证实“存在不同格方案”就不再参与后续比较，但枚举仍继续以统计方案总数。
    """
    steps = wafer.steps
    n = wafer.grid_size
    resolved: list[SameCellVerdict | None] = [None] * len(pairs)
    open_idx: list[int] = []
    defect_points: list[tuple[int, Defect]] = []
    for k, pair in enumerate(pairs):
        if not (0 <= pair.step_index_a < len(steps) and 0 <= pair.step_index_b < len(steps)):
            raise ValidationError("工序不存在")
        step_a, step_b = steps[pair.step_index_a], steps[pair.step_index_b]
        defect_a = next((d for d in step_a.defects if d.did == pair.did_a), None)
        defect_b = next((d for d in step_b.defects if d.did == pair.did_b), None)
        if defect_a is None:
            raise ValidationError(f"工序“{step_a.name}”中没有缺陷 {pair.did_a}")
        if defect_b is None:
            raise ValidationError(f"工序“{step_b.name}”中没有缺陷 {pair.did_b}")
        defect_points.append((pair.step_index_a, defect_a))
        open_idx.append(k)
        # 第二条记录放在 defect_b 的并行表里
        defect_points.append((pair.step_index_b, defect_b))

    common_cells: list[tuple[int, int] | None] = [None] * len(pairs)
    scheme_count = 0
    for scheme in iter_feasible_schemes(wafer):
        scheme_count += 1
        still_open: list[int] = []
        for k in open_idx:
            idx_a, defect_a = defect_points[2 * k]
            idx_b, defect_b = defect_points[2 * k + 1]
            cell_a = apply_transform(scheme[idx_a], defect_a.row, defect_a.col, n)
            cell_b = apply_transform(scheme[idx_b], defect_b.row, defect_b.col, n)
            if cell_a != cell_b:
                resolved[k] = SameCellVerdict(DIFFERENT_CELL, None, scheme_count)
                continue
            if common_cells[k] is None:
                common_cells[k] = cell_a
            elif cell_a != common_cells[k]:
                # 落点随方案变化但始终同格；置 None 表示“格不固定”。
                common_cells[k] = None
            still_open.append(k)
        open_idx = still_open

    if scheme_count == 0:
        return [SameCellVerdict(NO_SOLUTION, None, 0) for _ in pairs]
    return [
        resolved[k]
        if resolved[k] is not None
        else SameCellVerdict(SAME_CELL, common_cells[k], scheme_count)
        for k in range(len(pairs))
    ]
