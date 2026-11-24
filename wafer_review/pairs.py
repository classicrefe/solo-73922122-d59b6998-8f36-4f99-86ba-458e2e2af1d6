"""跨工序缺陷“同格”复查记录。

复查员在复查页选出同片晶圆的**两道不同工序**，各填一个缺陷编号。只有在
**每套全局可行方案**下，两条缺陷换算到基准坐标系后都落在同一格，才允许确认
同格（落在哪一格可以随方案变化，不把格子作为固定依据保存）。

同一对缺陷规范化为“工序 id 小者在前”后只保留一条记录，因此交换填写顺序命中
同一行。记录有两种状态：

* ``confirmed`` 已确认同格；
* ``pending`` 待复核：提交时不满足同格条件，或曾确认、在补点/改点/撤点后失去
  同格依据。即使后来定位点恢复、又能判为同格，也必须由人重新确认，状态不会
  自动回到 confirmed。

定位点写入与确认状态刷新在**同一个 SQLite 事务**里完成（由 service 层在事务
中调用 :func:`refresh_confirmed_pairs`），任一步失败整体回滚，定位点与确认
状态都沿用原值。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .db import utcnow
from .model import (
    SAME_CELL,
    PairRef,
    Step,
    ValidationError,
    Wafer,
    evaluate_pairs,
)

CONFIRMED = "confirmed"
PENDING = "pending"


@dataclass(frozen=True)
class PairRecord:
    id: int
    wafer_id: int
    step_id_a: int
    defect_a: str
    step_id_b: int
    defect_b: str
    status: str
    created_at: str
    updated_at: str

    def involves(self, step_id_a: int, defect_a: str, step_id_b: int, defect_b: str) -> bool:
        return (
            self.step_id_a == step_id_a
            and self.defect_a == defect_a
            and self.step_id_b == step_id_b
            and self.defect_b == defect_b
        )


def canonical(
    wafer: Wafer, step_id_a: int, defect_a: str, step_id_b: int, defect_b: str
) -> tuple[int, str, int, str]:
    """校验两道工序与两个缺陷，并把工序 id 小者规范到前面。"""
    defect_a = defect_a.strip()
    defect_b = defect_b.strip()
    if not defect_a or not defect_b:
        raise ValidationError("两个缺陷编号都必须填写")
    step_a = wafer.step_by_id(step_id_a)
    step_b = wafer.step_by_id(step_id_b)
    if step_a is None or step_b is None:
        raise ValidationError("工序不存在，请选择同片晶圆的两道工序")
    if step_a.id == step_b.id:
        raise ValidationError("同格复查要选两道不同的工序")
    if not any(d.did == defect_a for d in step_a.defects):
        raise ValidationError(f"工序“{step_a.name}”中没有缺陷 {defect_a}")
    if not any(d.did == defect_b for d in step_b.defects):
        raise ValidationError(f"工序“{step_b.name}”中没有缺陷 {defect_b}")
    if step_a.id < step_b.id:
        return step_a.id, defect_a, step_b.id, defect_b
    return step_b.id, defect_b, step_a.id, defect_a


def _to_ref(wafer: Wafer, step_id: int, did: str) -> tuple[int, Step]:
    step = wafer.step_by_id(step_id)
    assert step is not None
    return wafer.steps.index(step), step


def list_pairs(conn: sqlite3.Connection, wafer_id: int) -> list[PairRecord]:
    rows = conn.execute(
        "SELECT id, wafer_id, step_id_a, defect_a, step_id_b, defect_b, status, "
        "created_at, updated_at FROM defect_pair WHERE wafer_id = ? "
        "ORDER BY CASE status WHEN 'confirmed' THEN 0 ELSE 1 END, id",
        (wafer_id,),
    ).fetchall()
    return [
        PairRecord(
            id=r["id"],
            wafer_id=r["wafer_id"],
            step_id_a=r["step_id_a"],
            defect_a=r["defect_a"],
            step_id_b=r["step_id_b"],
            defect_b=r["defect_b"],
            status=r["status"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


def _pair_refs(wafer: Wafer, records: list[PairRecord]) -> list[PairRef]:
    refs: list[PairRef] = []
    for rec in records:
        idx_a, _ = _to_ref(wafer, rec.step_id_a, rec.defect_a)
        idx_b, _ = _to_ref(wafer, rec.step_id_b, rec.defect_b)
        refs.append(PairRef(idx_a, rec.defect_a, idx_b, rec.defect_b))
    return refs


def verdicts_for(wafer: Wafer, records: list[PairRecord]) -> dict[int, str]:
    """按当前定位点即时计算每条记录的同格判定状态（不写库）。

    返回 ``{record_id: SAME_CELL | DIFFERENT_CELL | NO_SOLUTION}``，供页面区分
    “待复核但现在已可重新确认”的记录。
    """
    if not records:
        return {}
    verdicts = evaluate_pairs(wafer, _pair_refs(wafer, records))
    return {rec.id: v.status for rec, v in zip(records, verdicts)}


def _evaluate_one(wafer: Wafer, sa: int, da: str, sb: int, db: str) -> str:
    idx_a, _ = _to_ref(wafer, sa, da)
    idx_b, _ = _to_ref(wafer, sb, db)
    return evaluate_pairs(wafer, [PairRef(idx_a, da, idx_b, db)])[0].status


def upsert_pair(
    conn: sqlite3.Connection,
    wafer: Wafer,
    step_id_a: int,
    defect_a: str,
    step_id_b: int,
    defect_b: str,
) -> tuple[PairRecord, str]:
    """提交（或对既有记录重新判定）一条缺陷对。

    同格条件成立 -> confirmed；否则记录保留为 pending，等复查员稍后重新确认。
    返回 ``(记录, 判定状态)``。调用方负责事务边界。
    """
    sa, da, sb, db = canonical(wafer, step_id_a, defect_a, step_id_b, defect_b)
    verdict = _evaluate_one(wafer, sa, da, sb, db)
    if verdict == "no_solution":
        raise ValidationError("当前定位点没有任何可行方案，无法判定同格")
    new_status = CONFIRMED if verdict == SAME_CELL else PENDING
    now = utcnow()
    existing = conn.execute(
        "SELECT id FROM defect_pair "
        "WHERE wafer_id = ? AND step_id_a = ? AND defect_a = ? "
        "AND step_id_b = ? AND defect_b = ?",
        (wafer.id, sa, da, sb, db),
    ).fetchone()
    if existing is None:
        cur = conn.execute(
            "INSERT INTO defect_pair (wafer_id, step_id_a, defect_a, step_id_b, defect_b, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (wafer.id, sa, da, sb, db, new_status, now, now),
        )
        pair_id = int(cur.lastrowid)
    else:
        pair_id = int(existing["id"])
        conn.execute(
            "UPDATE defect_pair SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, pair_id),
        )
    rec = next(r for r in list_pairs(conn, wafer.id) if r.id == pair_id)
    return rec, verdict


def delete_pair(conn: sqlite3.Connection, wafer_id: int, pair_id: int) -> None:
    cur = conn.execute(
        "DELETE FROM defect_pair WHERE id = ? AND wafer_id = ?", (pair_id, wafer_id)
    )
    if cur.rowcount != 1:
        raise ValidationError("复查记录不存在")


def refresh_confirmed_pairs(conn: sqlite3.Connection, wafer: Wafer) -> int:
    """按当前定位点复核本片晶圆**已确认**的缺陷对。

    失去同格依据（出现不同格方案，或已无可行方案）的 confirmed 记录转为
    pending；待复核记录保持原样，即使现在又能判同格也不自动确认。返回被降级
    的记录数。必须在定位点写操作的同一事务内调用。
    """
    confirmed = [r for r in list_pairs(conn, wafer.id) if r.status == CONFIRMED]
    if not confirmed:
        return 0
    verdicts = evaluate_pairs(wafer, _pair_refs(wafer, confirmed))
    now = utcnow()
    demoted = 0
    for rec, verdict in zip(confirmed, verdicts):
        if verdict.status != SAME_CELL:
            conn.execute(
                "UPDATE defect_pair SET status = ?, updated_at = ? WHERE id = ?",
                (PENDING, now, rec.id),
            )
            demoted += 1
    return demoted
