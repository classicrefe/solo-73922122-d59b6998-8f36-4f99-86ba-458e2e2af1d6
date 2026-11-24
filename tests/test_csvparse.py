"""CSV 解析的单元测试。"""
from __future__ import annotations

import pytest

from wafer_review.csvparse import CsvError, parse_csv


def test_chinese_header_and_kinds():
    text = "类型,编号,行,列\n定位点,L1,2,7\n缺陷,D101,4,4\n"
    parsed = parse_csv(text, 8)
    assert [r.ident for r in parsed.locators] == ["L1"]
    assert [r.ident for r in parsed.defects] == ["D101"]
    assert parsed.defects[0].row == 4 and parsed.defects[0].col == 4


def test_english_header_with_bom_and_comments():
    text = "﻿kind,id,row,col\n# 备注\nlocator,L1,1,1\ndefect,D1,8,8\n"
    parsed = parse_csv(text, 8)
    assert parsed.locators[0].ident == "L1"
    assert parsed.defects[0].ident == "D1"


def test_headerless_rows():
    text = "L,L1,2,2\nD,D1,3,3\n"
    parsed = parse_csv(text, 8)
    assert parsed.locators[0].row == 2
    assert parsed.defects[0].col == 3


def test_column_order_follows_header():
    text = "id,row,col,kind\nL1,2,7,locator\nD1,4,4,defect\n"
    parsed = parse_csv(text, 8)
    assert parsed.locators[0].col == 7
    assert parsed.defects[0].row == 4


@pytest.mark.parametrize(
    "text,message",
    [
        ("", "没有任何数据行"),
        ("类型,编号,行,列\n泡沫,L1,2,2\n", "类型无法识别"),
        ("类型,编号,行,列\n定位点,L1,2\n", "字段不足"),
        ("类型,编号,行,列\n定位点,L1,0,2\n", "超出"),
        ("类型,编号,行,列\n定位点,L1,2,9\n", "超出"),
        ("类型,编号,行,列\n定位点,L1,x,2\n", "不是整数"),
        ("类型,编号,行,列\n定位点,,2,2\n", "编号为空"),
        ("类型,编号,行,列\n定位点,L1,2,2\n定位点,L1,3,3\n", "编号重复"),
        ("类型,编号,行,列\n缺陷,D1,2,2\n缺陷,D1,3,3\n", "编号重复"),
    ],
)
def test_errors(text, message):
    with pytest.raises(CsvError) as exc:
        parse_csv(text, 8)
    assert message in str(exc.value)


def test_locator_and_defect_may_share_ident():
    parsed = parse_csv("类型,编号,行,列\n定位点,X,1,1\n缺陷,X,2,2\n", 8)
    assert parsed.locators[0].ident == parsed.defects[0].ident == "X"
