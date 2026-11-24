"""业务操作：上传建片、更正定位点、撤去定位点。

所有写操作都走同一个事务并在提交前用 ``compute_feasibility`` 校验。无解时整
个操作回滚，数据库保持原定位点，页面也重新显示原结果。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .csvparse import CsvError, ParsedCsv, parse_csv
from .db import connect, utcnow
from .model import (
    Defect,
    Locator,
    NoSolutionError,
    Step,
    ValidationError,
    Wafer,
    compute_feasibility,
)

MAX_STEPS = 6


@dataclass
class StepInput:
    name: str
    text: str


def _load_steps(conn: sqlite3.Connection, wafer_id: int) -> list[Step]:
    steps: list[Step] = []
    for row in conn.execute(
        "SELECT id, seq, name FROM step WHERE wafer_id = ? ORDER BY seq", (wafer_id,)
    ):
        locators = [
            Locator(r["id"], r["pid"], r["row"], r["col"])
            for r in conn.execute(
                "SELECT id, pid, row, col FROM locator WHERE step_id = ? ORDER BY pid",
                (row["id"],),
            )
        ]
        defects = [
            Defect(r["id"], r["did"], r["row"], r["col"])
            for r in conn.execute(
                "SELECT id, did, row, col FROM defect WHERE step_id = ? ORDER BY did",
                (row["id"],),
            )
        ]
        steps.append(Step(row["id"], row["seq"], row["name"], locators, defects))
    return steps


def load_wafer(conn: sqlite3.Connection, wafer_id: int) -> Wafer | None:
    row = conn.execute(
        "SELECT id, name, grid_size, revision, created_at, updated_at FROM wafer WHERE id = ?",
        (wafer_id,),
    ).fetchone()
    if row is None:
        return None
    return Wafer(
        id=row["id"],
        name=row["name"],
        grid_size=row["grid_size"],
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        steps=_load_steps(conn, wafer_id),
    )


def list_wafers(conn: sqlite3.Connection) -> list[Wafer]:
    rows = conn.execute(
        "SELECT id FROM wafer ORDER BY id DESC"
    ).fetchall()
    return [w for w in (load_wafer(conn, r["id"]) for r in rows) if w is not None]


def _normalize_step_names(steps: list[StepInput]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for i, step in enumerate(steps, start=1):
        name = step.name.strip()
        if not name:
            raise ValidationError(f"第 {i} 道工序没有名称")
        if name in seen:
            raise ValidationError(f"工序名称重复：{name}")
        seen.add(name)
        names.append(name)
    return names


def _parse_steps(steps: list[StepInput], grid_size: int) -> list[ParsedCsv]:
    parsed = []
    for i, step in enumerate(steps, start=1):
        try:
            parsed.append(parse_csv(step.text, grid_size))
        except CsvError as exc:
            raise ValidationError(f"第 {i} 道工序（{step.name}）CSV 有误：{exc}") from exc
    return parsed


def _insert_records(conn: sqlite3.Connection, step_id: int, parsed: ParsedCsv) -> None:
    conn.executemany(
        "INSERT INTO locator (step_id, pid, row, col) VALUES (?, ?, ?, ?)",
        [(step_id, rec.ident, rec.row, rec.col) for rec in parsed.locators],
    )
    conn.executemany(
        "INSERT INTO defect (step_id, did, row, col) VALUES (?, ?, ?, ?)",
        [(step_id, rec.ident, rec.row, rec.col) for rec in parsed.defects],
    )


def create_wafer(db_path: str | Path, name: str, grid_size: int, steps: list[StepInput]) -> int:
    name = name.strip()
    if not name:
        raise ValidationError("晶圆名称不能为空")
    if not (2 <= grid_size <= 60):
        raise ValidationError("每边格数必须在 2 到 60 之间")
    if not steps:
        raise ValidationError("至少上传一道工序")
    if len(steps) > MAX_STEPS:
        raise ValidationError(f"最多上传 {MAX_STEPS} 道工序")

    names = _normalize_step_names(steps)
    parsed = _parse_steps(steps, grid_size)

    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = utcnow()
            cur = conn.execute(
                "INSERT INTO wafer (name, grid_size, revision, created_at, updated_at) "
                "VALUES (?, ?, 0, ?, ?)",
                (name, grid_size, now, now),
            )
            wafer_id = int(cur.lastrowid)
            for seq, (step_name, records) in enumerate(zip(names, parsed), start=1):
                cur = conn.execute(
                    "INSERT INTO step (wafer_id, seq, name) VALUES (?, ?, ?)",
                    (wafer_id, seq, step_name),
                )
                _insert_records(conn, int(cur.lastrowid), records)
            wafer = load_wafer(conn, wafer_id)
            assert wafer is not None
            if not compute_feasibility(wafer).solvable:
                raise NoSolutionError(
                    "各工序无法在同一套变换下让同号定位点全部重合，未保存本次上传。"
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return wafer_id


def _reload(conn: sqlite3.Connection, wafer_id: int) -> Wafer:
    wafer = load_wafer(conn, wafer_id)
    if wafer is None:
        raise ValidationError("晶圆不存在")
    return wafer


def _assert_solvable(conn: sqlite3.Connection, wafer_id: int) -> None:
    wafer = _reload(conn, wafer_id)
    if not compute_feasibility(wafer).solvable:
        raise NoSolutionError(
            "改动后不存在任何可行方案，已保留原定位点与原查询结果。"
        )


def _bump(conn: sqlite3.Connection, wafer_id: int) -> None:
    conn.execute(
        "UPDATE wafer SET revision = revision + 1, updated_at = ? WHERE id = ?",
        (utcnow(), wafer_id),
    )


def correct_locator(
    db_path: str | Path, wafer_id: int, locator_id: int, row: int, col: int
) -> None:
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            wafer = _reload(conn, wafer_id)
            if not (1 <= row <= wafer.grid_size and 1 <= col <= wafer.grid_size):
                raise ValidationError("修正后的行列超出网格范围")
            cur = conn.execute(
                "UPDATE locator SET row = ?, col = ? "
                "WHERE id = ? AND step_id IN (SELECT id FROM step WHERE wafer_id = ?)",
                (row, col, locator_id, wafer_id),
            )
            if cur.rowcount != 1:
                raise ValidationError("定位点不存在")
            _assert_solvable(conn, wafer_id)
            _bump(conn, wafer_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def remove_locator(db_path: str | Path, wafer_id: int, locator_id: int) -> None:
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "DELETE FROM locator "
                "WHERE id = ? AND step_id IN (SELECT id FROM step WHERE wafer_id = ?)",
                (locator_id, wafer_id),
            )
            if cur.rowcount != 1:
                raise ValidationError("定位点不存在")
            _assert_solvable(conn, wafer_id)
            _bump(conn, wafer_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def add_locator(db_path: str | Path, wafer_id: int, step_id: int, pid: str, row: int, col: int) -> None:
    """在既有工序上补一个定位点（演示与纠错时有用）。"""
    pid = pid.strip()
    if not pid:
        raise ValidationError("定位点编号不能为空")
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            wafer = _reload(conn, wafer_id)
            if wafer.step_by_id(step_id) is None:
                raise ValidationError("工序不存在")
            if not (1 <= row <= wafer.grid_size and 1 <= col <= wafer.grid_size):
                raise ValidationError("行列超出网格范围")
            if any(loc.pid == pid for loc in wafer.step_by_id(step_id).locators):
                raise ValidationError(f"该工序已有定位点 {pid}，请先撤去旧点")
            conn.execute(
                "INSERT INTO locator (step_id, pid, row, col) VALUES (?, ?, ?, ?)",
                (step_id, pid, row, col),
            )
            _assert_solvable(conn, wafer_id)
            _bump(conn, wafer_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def delete_wafer(db_path: str | Path, wafer_id: int) -> None:
    with connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM wafer WHERE id = ?", (wafer_id,))
