"""生成 samples/ 下的演示 CSV（与 wafer_review.demo 保持一致）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wafer_review.demo import demo_samples  # noqa: E402


def main() -> None:
    out = ROOT / "samples"
    out.mkdir(exist_ok=True)
    for index, (name, text) in enumerate(demo_samples(), start=1):
        path = out / f"{index}_{name}.csv"
        path.write_text(text, encoding="utf-8", newline="")
        print(path)


if __name__ == "__main__":
    main()
