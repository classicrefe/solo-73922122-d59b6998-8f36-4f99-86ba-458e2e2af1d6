"""CSV 解析。

每个 CSV 代表一道工序，每行是“定位点”或“缺陷”，字段为：

    类型,编号,行,列

类型用 ``定位点`` / ``缺陷``（也接受 ``locator`` / ``defect``、``L`` / ``D``）。
表头可有可无；中文或英文表头均可。空行与 ``#`` 开头的注释行会被忽略。

编号在整片晶圆内跨工序复用：同一个定位点编号表示不同工序里的同一个物理标记。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Literal

Kind = Literal["locator", "defect"]

LOCATOR_ALIASES = {"定位点", "定位", "locator", "loc", "l"}
DEFECT_ALIASES = {"缺陷", "defect", "def", "d"}
ID_ALIASES = {"编号", "id", "名称", "name", "代号"}
ROW_ALIASES = {"行", "row", "r"}
COL_ALIASES = {"列", "col", "column", "c"}
TYPE_ALIASES = {"类型", "kind", "type", "类别"}

HEADER_ALIASES = {
    "kind": TYPE_ALIASES,
    "id": ID_ALIASES,
    "row": ROW_ALIASES,
    "col": COL_ALIASES,
}


class CsvError(ValueError):
    pass


@dataclass(frozen=True)
class CsvRecord:
    kind: Kind
    ident: str
    row: int
    col: int
    line: int


@dataclass(frozen=True)
class ParsedCsv:
    locators: tuple[CsvRecord, ...]
    defects: tuple[CsvRecord, ...]


def _norm(token: str) -> str:
    return token.strip().lstrip("﻿").casefold()


def _classify_kind(token: str) -> Kind | None:
    t = _norm(token)
    if t in LOCATOR_ALIASES:
        return "locator"
    if t in DEFECT_ALIASES:
        return "defect"
    return None


def _guess_columns(header: list[str]) -> dict[str, int] | None:
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(header):
        t = _norm(cell)
        for field, aliases in HEADER_ALIASES.items():
            if t in aliases and field not in mapping:
                mapping[field] = idx
    if set(mapping) == {"kind", "id", "row", "col"}:
        return mapping
    return None


def parse_csv(text: str, n: int) -> ParsedCsv:
    """解析一道工序的 CSV。任何格式错误都抛出 CsvError。"""
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.reader(io.StringIO(text))
    locators: list[CsvRecord] = []
    defects: list[CsvRecord] = []
    columns: dict[str, int] | None = None
    seen_locators: set[str] = set()
    seen_defects: set[str] = set()
    saw_data = False

    for lineno, raw in enumerate(reader, start=1):
        if not raw or not any(cell.strip() for cell in raw):
            continue
        if raw[0].lstrip().startswith("#"):
            continue

        if not saw_data and (guessed := _guess_columns(raw)) is not None:
            columns = guessed
            continue

        saw_data = True
        fields = columns if columns is not None else {"kind": 0, "id": 1, "row": 2, "col": 3}
        max_idx = max(fields.values())
        if len(raw) <= max_idx:
            raise CsvError(f"第 {lineno} 行字段不足")
        kind = _classify_kind(raw[fields["kind"]])
        ident = raw[fields["id"]].strip()
        row_text = raw[fields["row"]].strip()
        col_text = raw[fields["col"]].strip()

        if kind is None:
            raise CsvError(f"第 {lineno} 行类型无法识别：{raw[fields['kind']]!r}")
        if not ident:
            raise CsvError(f"第 {lineno} 行编号为空")
        try:
            row = int(row_text)
            col = int(col_text)
        except ValueError as exc:
            raise CsvError(f"第 {lineno} 行行列不是整数：{row_text!r}, {col_text!r}") from exc
        if not (1 <= row <= n and 1 <= col <= n):
            raise CsvError(f"第 {lineno} 行行列超出 1..{n} 网格：{row}, {col}")

        if kind == "locator":
            if ident in seen_locators:
                raise CsvError(f"第 {lineno} 行定位点编号重复：{ident}")
            seen_locators.add(ident)
            locators.append(CsvRecord(kind, ident, row, col, lineno))
        else:
            if ident in seen_defects:
                raise CsvError(f"第 {lineno} 行缺陷编号重复：{ident}")
            seen_defects.add(ident)
            defects.append(CsvRecord(kind, ident, row, col, lineno))

    if not saw_data:
        raise CsvError("CSV 没有任何数据行")
    return ParsedCsv(tuple(locators), tuple(defects))
