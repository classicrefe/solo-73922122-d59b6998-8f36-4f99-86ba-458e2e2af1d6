"""HTTP 层测试：上传、查询、撤点、失败回退。"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from wafer_review.app import create_app
from wafer_review.db import connect
from wafer_review.model import analyze
from wafer_review.service import load_wafer

SAMPLES = [
    ("光刻", "1_光刻.csv"),
    ("刻蚀", "2_刻蚀.csv"),
    ("沉积", "3_沉积.csv"),
    ("研磨", "4_研磨.csv"),
    ("检测", "5_检测.csv"),
    ("封装", "6_封装.csv"),
]
SAMPLE_DIR = Path("/workspace/samples")


@pytest.fixture()
def app(tmp_path):
    return create_app({"DATABASE": str(tmp_path / "app.sqlite3"), "TESTING": True})


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, grid="8", count=6):
    data = {"name": "LOT-HTTP", "grid_size": grid}
    for i, (name, filename) in enumerate(SAMPLES[:count], start=1):
        data[f"name_{i}"] = name
        data[f"csv_{i}"] = (
            io.BytesIO((SAMPLE_DIR / filename).read_bytes()),
            filename,
        )
    return client.post("/wafers", data=data, content_type="multipart/form-data")


def wafer_of(app):
    with connect(app.config["DATABASE"]) as conn:
        return load_wafer(conn, 1)


def test_index_and_new_pages(client):
    assert client.get("/").status_code == 200
    page = client.get("/wafers/new")
    assert page.status_code == 200
    assert "每边格数" in page.get_data(as_text=True)


def test_upload_shows_queries(app, client):
    resp = upload(client, count=6)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/wafers/1")
    page = client.get("/wafers/1")
    body = page.get_data(as_text=True)
    assert "LOT-HTTP" in body
    assert "共 2 套可行方案" in body
    assert "待定" in body  # 封装 D102/D104

    detail = client.get("/wafers/1?step=6&did=D102")
    assert "待定" in detail.get_data(as_text=True)
    determined = client.get("/wafers/1?step=6&did=D101")
    assert "第 4 行" in determined.get_data(as_text=True)


def test_api_lists_all_results(app, client):
    upload(client)
    payload = client.get("/api/wafers/1/defects").get_json()
    assert payload["grid_size"] == 8
    assert payload["scheme_count"] == 2
    by_step = {}
    for row in payload["results"]:
        by_step.setdefault(row["step"], []).append(row)
    assert by_step["封装"][0]["defect"] == "D101"
    statuses = {r["defect"]: r["status"] for r in by_step["封装"]}
    assert statuses["D102"] == "pending"
    assert statuses["D101"] == "determined"


def test_remove_locator_changes_other_step_results(app, client):
    upload(client)
    wafer = wafer_of(app)
    c1 = next(loc for loc in wafer.steps[2].locators if loc.pid == "C1")
    resp = client.post(
        f"/wafers/1/locators/{c1.id}/remove",
        data={"step": str(wafer.steps[2].id), "did": "D101"},
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "定位点已撤去" in body
    assert "待定" in body  # 封装的 D101 现在待定

    wafer_after = wafer_of(app)
    assert wafer_after.revision == 1
    assert not any(loc.pid == "C1" for loc in wafer_after.steps[2].locators)


def test_failed_correction_keeps_old_state(app, client):
    upload(client)
    wafer = wafer_of(app)
    c1 = wafer.steps[5].locators[0]
    resp = client.post(
        f"/wafers/1/locators/{c1.id}/correct",
        data={"step": "6", "did": "D102", "row": "1", "col": "1"},
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "未生效" in body
    wafer_after = wafer_of(app)
    assert (wafer_after.steps[5].locators[0].row, wafer_after.steps[5].locators[0].col) == (4, 5)
    assert wafer_after.revision == 0
    # 原查询结果仍在
    assert analyze(wafer_after).query(wafer_after.steps[5], "D104").status == "pending"


def test_correction_updates_results(app, client):
    upload(client)
    wafer = wafer_of(app)
    c1 = wafer.steps[5].locators[0]
    resp = client.post(
        f"/wafers/1/locators/{c1.id}/correct",
        data={"step": "6", "did": "D104", "row": "4", "col": "4"},
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "定位点已更正" in body
    wafer_after = wafer_of(app)
    result = analyze(wafer_after).query(wafer_after.steps[5], "D104")
    assert result.status == "determined"
    assert result.coordinate == (5, 5)


def test_add_locator_through_http(app, client):
    upload(client)
    wafer = wafer_of(app)
    resp = client.post(
        "/wafers/1/locators",
        data={"step_id": str(wafer.steps[5].id), "step": "6", "did": "D101", "pid": "C9", "row": "4", "col": "4"},
        follow_redirects=True,
    )
    assert "定位点已补充" in resp.get_data(as_text=True)
    assert any(loc.pid == "C9" for loc in wafer_of(app).steps[5].locators)


def test_bad_upload_inputs_keep_user_on_form(client):
    resp = client.post("/wafers", data={"name": "X", "grid_size": "abc"}, follow_redirects=True)
    assert "每边格数必须是整数" in resp.get_data(as_text=True)

    resp = client.post("/wafers", data={"name": "X", "grid_size": "8"}, follow_redirects=True)
    assert "至少上传一道工序" in resp.get_data(as_text=True)

    data = {
        "name": "X",
        "grid_size": "8",
        "name_1": "a",
        "csv_text_1": "类型,编号,行,列\n缺陷,D1,9,9\n",
    }
    resp = client.post("/wafers", data=data, follow_redirects=True)
    assert "CSV 有误" in resp.get_data(as_text=True)


def test_unknown_wafer_returns_404(client):
    assert client.get("/wafers/999").status_code == 404
    assert client.get("/api/wafers/999/defects").status_code == 404


def test_delete_wafer(client):
    upload(client)
    resp = client.post("/wafers/1/delete", follow_redirects=True)
    assert "晶圆记录已删除" in resp.get_data(as_text=True)
    assert client.get("/wafers/1").status_code == 404


def test_healthz_and_samples(client):
    assert client.get("/healthz").get_json() == {"status": "ok"}
    resp = client.get("/samples/3.csv")
    assert resp.status_code == 200
    assert "定位点,C1,4,5" in resp.get_data(as_text=True)
    assert client.get("/samples/99.csv").status_code == 404


def test_demo_route_creates_wafer(client):
    resp = client.post("/wafers/demo", follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "演示晶圆已建立" in body
    assert "共 2 套可行方案" in body


def test_locators_api(app, client):
    upload(client)
    payload = client.get("/api/wafers/1/locators").get_json()
    assert payload["scheme_count"] == 2
    assert [s["name"] for s in payload["steps"]][0] == "光刻"
    packing = payload["steps"][5]
    assert packing["transforms"] == ["顺时针270°", "左右镜像"]
    assert [loc["pid"] for loc in packing["locators"]] == ["C1"]
