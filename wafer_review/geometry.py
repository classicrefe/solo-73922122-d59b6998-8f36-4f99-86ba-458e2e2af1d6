"""晶圆网格上的 D4 变换。

坐标一律使用 1 起始的 (行, 列)，范围 1..n。变换的语义是“把某道工序图上的
坐标换算到基准图坐标系”：

* 旋转：绕外接正方形中心顺时针旋转 90° 的整数倍；
* 镜像：在旋转之后左右镜像（列 c -> n + 1 - c）。

旋转 4 种、镜像 2 种，组合起来正好是正方形的全部 8 种对称变换（4 个纯旋转 +
4 个镜像）。因此“旋转 90° 的整数倍、镜像或两者叠加”恰好就是这 8 种。

之所以把镜像放在旋转之后：若先镜像再旋转，对正方形的 D4 群虽然等价（只是把
(镜像, 旋转 k) 换成 (镜像, 旋转 4-k)），但把镜像放在最后，能让“同一镜像 + 不同
旋转”保持直观的旋转叠加关系，也避免在 90° 与 270° 之间出现意外的重合。
"""
from __future__ import annotations

from typing import Iterable

# 变换表示为 (是否左右镜像, 顺时针旋转次数 0..3)
Transform = tuple[bool, int]

ALL_TRANSFORMS: tuple[Transform, ...] = tuple(
    (mirrored, turns) for mirrored in (False, True) for turns in range(4)
)
IDENTITY: Transform = (False, 0)

_LABELS: dict[Transform, str] = {
    (False, 0): "原样",
    (False, 1): "顺时针90°",
    (False, 2): "旋转180°",
    (False, 3): "顺时针270°",
    (True, 0): "左右镜像",
    (True, 1): "镜像+顺时针90°",
    (True, 2): "镜像+旋转180°",
    (True, 3): "镜像+顺时针270°",
}


def apply_transform(transform: Transform, row: int, col: int, n: int) -> tuple[int, int]:
    """把工序图坐标换算到基准图坐标。

    先把点绕外接正方形中心顺时针旋转 ``turns * 90°``，再按需左右镜像。
    这两种操作各 4 个取值，组合起来正好是正方形的 8 种对称变换。
    """
    mirrored, turns = transform
    r, c = row, col
    for _ in range(turns):
        r, c = c, n + 1 - r
    if mirrored:
        c = n + 1 - c
    return r, c


def invert_transform(transform: Transform, row: int, col: int, n: int) -> tuple[int, int]:
    """apply_transform 的逆变换（基准图坐标 -> 工序图坐标）。"""
    mirrored, turns = transform
    r, c = row, col
    if mirrored:
        c = n + 1 - c
    for _ in range((4 - turns) % 4):
        r, c = c, n + 1 - r
    return r, c


def transform_label(transform: Transform) -> str:
    return _LABELS[transform]


def transform_labels(transforms: Iterable[Transform]) -> list[str]:
    order = {t: i for i, t in enumerate(ALL_TRANSFORMS)}
    return [transform_label(t) for t in sorted(set(transforms), key=order.get)]  # type: ignore[arg-type]


def is_in_grid(row: int, col: int, n: int) -> bool:
    return 1 <= row <= n and 1 <= col <= n
