"""WSGI 入口：gunicorn 使用 ``wsgi:app``。"""
from __future__ import annotations

from wafer_review.app import create_app

app = create_app()
