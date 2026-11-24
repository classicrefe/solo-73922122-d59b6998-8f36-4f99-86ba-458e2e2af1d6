"""晶圆缺陷坐标复查。"""

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    from .app import create_app as factory

    return factory(*args, **kwargs)
