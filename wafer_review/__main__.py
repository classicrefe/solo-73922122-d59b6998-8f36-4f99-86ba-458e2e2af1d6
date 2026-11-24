"""`python -m wafer_review` 启动服务器。"""
from __future__ import annotations

import os


def main() -> None:
    from .app import create_app

    app = create_app()
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        debug=False,
    )


if __name__ == "__main__":
    main()
