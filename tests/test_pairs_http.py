"""同格复查的 HTTP 层测试：页面表单、确认、撤点联动、重新确认、删除。"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from wafer_review.app import create_app
from wafer_review.db import connect
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
    return create_app({"DATABASE": str(tmp_path / "pairs-http.sqlite3"), "TESTING": True})


@pytest.fixture()
def client(app):
    return app.test_client()


def _upload(client):
    data = {"name": "LOT-PAIR", "grid_size": "8"}
    for i, (name, filename) in enumerate(SAMPLES, start=1):
        data[f"name_{i}"] = name
        data[f"csv_{i}"] = (
            io.BytesIO((SAMPLE_DIR / filename).read_bytes()),
            filename,
        )
    resp = client.post("/wafers", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302
    return 1


def _submit(client, wid, sa, da, sb, db_):
    return client.post(
        f"/wafers/{wid}/pairs",
        data={
            "pair_step_a": str(sa),
            "pair_defect_a": da,
            "pair_step_b": str(sb),
            "pair_defect_b": db_,
        },
        follow_redirects=True,
    )


def test_review_panel_renders(client):
    wid = _upload(client)
    body = client.get(f"/wafers/{wid}").get_data(as_text=True)
    assert "同格复查" in body
    assert "每套全局可行方案" in body
    assert f'action="/wafers/{wid}/pairs"' in body


def test_confirm_same_cell_pair_and_dedup_swap(client):
    wid = _upload(client)
    resp = _submit(client, wid, 1, "D101", 2, "D101")
    assert "已确认同格" in resp.get_data(as_text=True)
    payload = client.get(f"/api/wafers/{wid}/pairs").get_json()
    assert len(payload["pairs"]) == 1
    assert payload["pairs"][0]["status"] == "confirmed"
    assert payload["pairs"][0]["live_verdict"] == "same_cell"

    # 交换顺序再填一次：仍是同一条记录
    _submit(client, wid, 2, "D101", 1, "D101")
    payload = client.get(f"/api/wafers/{wid}/pairs").get_json()
    assert len(payload["pairs"]) == 1


def test_pending_pair_is_listed_with_live_verdict(client):
    wid = _upload(client)
    resp = _submit(client, wid, 1, "D101", 6, "D102")
    assert "待复核" in resp.get_data(as_text=True)
    payload = client.get(f"/api/wafers/{wid}/pairs").get_json()
    pair = payload["pairs"][0]
    assert pair["status"] == "pending"
    assert pair["live_verdict"] == "different"
    # 页面可查待复核记录
    body = client.get(f"/wafers/{wid}").get_data(as_text=True)
    assert "待复核" in body and "D102" in body


def test_reject_same_step_and_unknown_defect(client):
    wid = _upload(client)
    resp = _submit(client, wid, 1, "D101", 1, "D102")
    assert "两道不同的工序" in resp.get_data(as_text=True)
    resp = _submit(client, wid, 1, "NOPE", 2, "D101")
    assert "没有缺陷 NOPE" in resp.get_data(as_text=True)
    assert client.get(f"/api/wafers/{wid}/pairs").get_json()["pairs"] == []


def test_locator_remove_demotes_pair_with_flash(client, app):
    wid = _upload(client)
    _submit(client, wid, 1, "D101", 6, "D101")
    with connect(app.config["DATABASE"]) as conn:
        wafer = load_wafer(conn, wid)
    c1 = next(loc.id for loc in wafer.steps[2].locators if loc.pid == "C1")
    resp = client.post(
        f"/wafers/{wid}/locators/{c1}/remove",
        data={"step": str(wafer.steps[2].id)},
        follow_redirects=True,
    )
    assert "1 条已确认的同格记录失去依据，已转为待复核" in resp.get_data(as_text=True)
    pair = client.get(f"/api/wafers/{wid}/pairs").get_json()["pairs"][0]
    assert pair["status"] == "pending" and pair["live_verdict"] == "different"


def test_reconfirm_after_restoring_locator(client, app):
    wid = _upload(client)
    _submit(client, wid, 1, "D101", 6, "D101")
    with connect(app.config["DATABASE"]) as conn:
        wafer = load_wafer(conn, wid)
    c1 = next(loc.id for loc in wafer.steps[2].locators if loc.pid == "C1")
    client.post(f"/wafers/{wid}/locators/{c1}/remove", data={"step": "3"})
    dep_step = wafer.steps[2].id
    client.post(
        f"/wafers/{wid}/locators",
        data={"step_id": str(dep_step), "pid": "C1", "row": "4", "col": "5"},
    )
    pid = client.get(f"/api/wafers/{wid}/pairs").get_json()["pairs"][0]["id"]
    # 恢复后状态仍为待复核，必须人工重新确认
    assert client.get(f"/api/wafers/{wid}/pairs").get_json()["pairs"][0]["status"] == "pending"
    resp = client.post(f"/wafers/{wid}/pairs/{pid}/reconfirm", data={}, follow_redirects=True)
    assert "已重新确认同格" in resp.get_data(as_text=True)
    pair = client.get(f"/api/wafers/{wid}/pairs").get_json()["pairs"][0]
    assert pair["status"] == "confirmed" and pair["live_verdict"] == "same_cell"


def test_reconfirm_when_not_same_cell_keeps_pending(client, app):
    wid = _upload(client)
    _submit(client, wid, 1, "D101", 6, "D102")  # 本就待复核且不同格
    pid = client.get(f"/api/wafers/{wid}/pairs").get_json()["pairs"][0]["id"]
    resp = client.post(f"/wafers/{wid}/pairs/{pid}/reconfirm", data={}, follow_redirects=True)
    assert "保持待复核" in resp.get_data(as_text=True)
    assert client.get(f"/api/wafers/{wid}/pairs").get_json()["pairs"][0]["status"] == "pending"


def test_delete_pair(client):
    wid = _upload(client)
    _submit(client, wid, 1, "D101", 2, "D101")
    pid = client.get(f"/api/wafers/{wid}/pairs").get_json()["pairs"][0]["id"]
    resp = client.post(f"/wafers/{wid}/pairs/{pid}/delete", data={}, follow_redirects=True)
    assert "复查记录已删除" in resp.get_data(as_text=True)
    assert client.get(f"/api/wafers/{wid}/pairs").get_json()["pairs"] == []


def test_pair_routes_404_for_unknown_wafer(client):
    assert client.get("/api/wafers/999/pairs").status_code == 404
    resp = _submit(client, 999, 1, "D101", 2, "D101")
    assert resp.status_code == 404


def test_malformed_step_input_is_rejected(client):
    wid = _upload(client)
    resp = client.post(
        f"/wafers/{wid}/pairs",
        data={"pair_step_a": "x", "pair_defect_a": "D101",
              "pair_step_b": "2", "pair_defect_b": "D101"},
        follow_redirects=True,
    )
    assert "请选择两道工序" in resp.get_data(as_text=True)
