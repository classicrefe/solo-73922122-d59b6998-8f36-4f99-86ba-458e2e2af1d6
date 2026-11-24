"""几何变换的单元测试。"""
from __future__ import annotations

import itertools

import pytest

from wafer_review.geometry import (
    ALL_TRANSFORMS,
    IDENTITY,
    apply_transform,
    invert_transform,
    is_in_grid,
    transform_label,
)


@pytest.mark.parametrize("n", [2, 3, 8, 9])
def test_identity_is_noop(n):
    for r, c in itertools.product(range(1, n + 1), repeat=2):
        assert apply_transform(IDENTITY, r, c, n) == (r, c)


@pytest.mark.parametrize(
    "transform,expected",
    [
        ((False, 1), (7, 7)),  # (2,7) 顺时针 90°
        ((False, 2), (7, 2)),
        ((False, 3), (2, 2)),
        ((True, 0), (2, 2)),
        ((True, 1), (7, 2)),
    ],
)
def test_reference_cells(transform, expected):
    assert apply_transform(transform, 2, 7, 8) == expected


def test_eight_distinct_transforms():
    # (2,6) 不落在任何一条镜像轴上，轨道长度为 8
    cells = {(apply_transform(t, 2, 6, 8)) for t in ALL_TRANSFORMS}
    assert len(cells) == 8
    assert len(set(ALL_TRANSFORMS)) == 8


@pytest.mark.parametrize("n", [2, 5, 8])
@pytest.mark.parametrize("transform", ALL_TRANSFORMS)
def test_inverse_roundtrip(n, transform):
    for r, c in itertools.product(range(1, n + 1), repeat=2):
        rr, cc = apply_transform(transform, r, c, n)
        assert is_in_grid(rr, cc, n)
        assert invert_transform(transform, rr, cc, n) == (r, c)


def test_labels_unique():
    labels = [transform_label(t) for t in ALL_TRANSFORMS]
    assert len(set(labels)) == 8
    assert "原样" in labels
    assert "左右镜像" in labels
