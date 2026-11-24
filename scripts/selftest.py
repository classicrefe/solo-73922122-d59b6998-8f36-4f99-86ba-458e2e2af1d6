"""发布前自检：脚本化走一遍关键操作，并核对页面/接口输出。

运行方式（容器内或本机均可）：

    python scripts/selftest.py                                  # 用临时数据库
    BASE_URL=http://127.0.0.1:8000 python scripts/selftest.py   # 打真实服务

BASE_URL 模式需要服务端已有演示数据或允许创建；脚本会新建一张名为 SELF-TEST 的晶圆，
并使用返回的 id（通过 ``/api/wafers`` 查询）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class Browser:
    """极简 HTTP 客户端；本地模式走 Flask test_client，不启真实端口。"""

    def __init__(self, base_url: str | None) -> None:
        self.base_url = base_url
        self.client = None
        # flash 提示存在签名 session cookie 里，跟随重定向时必须把 cookie 带回去
        self.cookie_jar = urllib.request.HTTPCookieProcessor()

    def _send(self, method: str, path: str, body: bytes | None, headers: dict) -> tuple[int, str, dict]:
        if self.base_url:
            # path 可能已是绝对 URL（跟随真实服务回的绝对 Location），不要再拼前缀
            url = path if path.startswith(("http://", "https://")) else self.base_url + path
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            # 不自动跟随重定向，才能拿到上传/改点后的 Location；cookie jar 负责回传 session
            opener = urllib.request.build_opener(self.cookie_jar, _NoRedirect)
            try:
                with opener.open(req, timeout=20) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)
        assert self.client is not None
        resp = self.client.open(
            path,
            method=method,
            data=body,
            headers=headers,
            follow_redirects=False,
        )
        return resp.status_code, resp.get_data(as_text=True), dict(resp.headers)

    def get(self, path: str) -> tuple[int, str]:
        status, body, _ = self._send("GET", path, None, {})
        return status, body

    def post_form(self, path: str, fields: dict[str, str]) -> tuple[int, str]:
        body = urllib.parse.urlencode(fields).encode()
        status, text, _ = self._send(
            "POST", path, body, {"Content-Type": "application/x-www-form-urlencoded"}
        )
        return status, text

    def post_form_follow(self, path: str, fields: dict[str, str]) -> tuple[int, str]:
        """POST 后再 GET 一次重定向目标，才能读到基于 session 的 flash 提示。"""
        body = urllib.parse.urlencode(fields).encode()
        status, text, headers = self._send(
            "POST", path, body, {"Content-Type": "application/x-www-form-urlencoded"}
        )
        if status in (302, 303) and headers.get("Location"):
            location = headers["Location"]
            if self.base_url and location.startswith("/"):
                target = self.base_url + location
            else:
                # 绝对 URL（真实服务常回带主机名的 Location）直接使用
                target = location
            return self.get(target)
        return status, text
    def post_files(
        self, path: str, fields: dict[str, str], files: list[tuple[str, str, bytes]]
    ) -> tuple[int, str, dict]:
        boundary = "----selftest" + uuid.uuid4().hex
        chunks: list[bytes] = []
        for key, value in fields.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )
        for key, filename, content in files:
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
                f'filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n'.encode()
            )
            chunks.append(content + b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        status, text, headers = self._send(
            "POST",
            path,
            b"".join(chunks),
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        return status, text, headers


def build_browser(base_url: str | None) -> Browser:
    browser = Browser(base_url)
    if base_url is None:
        from wafer_review.app import create_app

        tmp = tempfile.mkdtemp(prefix="wafer-selftest-")
        app = create_app({"DATABASE": str(Path(tmp) / "selftest.sqlite3"), "TESTING": True})
        browser.client = app.test_client()
    return browser


def demo_csvs() -> list[tuple[str, str, bytes]]:
    from wafer_review.demo import demo_samples

    return [
        (f"csv_{i}", f"{i}_{name}.csv", text.encode("utf-8"))
        for i, (name, text) in enumerate(demo_samples(), start=1)
    ]


def demo_names() -> list[str]:
    from wafer_review.demo import demo_samples

    return [name for name, _ in demo_samples()]


def _review_wafer_fields() -> dict[str, str]:
    return {
        "name": "SELF-TEST-PAIR-REVIEW",
        "grid_size": "8",
        **{f"name_{i}": name for i, name in enumerate(demo_names(), start=1)},
    }


def api(browser: Browser, path: str):
    status, body = browser.get(path)
    if status != 200:
        raise RuntimeError(f"{path} -> HTTP {status}")
    return json.loads(body)


def main() -> int:
    base_url = os.environ.get("BASE_URL", "").rstrip("/") or None
    browser = build_browser(base_url)

    status, body = browser.get("/")
    check("首页可访问", status == 200, f"HTTP {status}")
    check("首页有新建入口", "新建复查" in body)

    fields = {"name": "SELF-TEST", "grid_size": "8"}
    for i, name in enumerate(demo_names(), start=1):
        fields[f"name_{i}"] = name
    status, _text, headers = browser.post_files("/wafers", fields, demo_csvs())
    location = headers.get("Location", "")
    match = re.search(r"/wafers/(\d+)$", location)
    check("上传 6 道工序成功", status in (302, 303) and match is not None, f"HTTP {status} {location}")
    if match is None:
        print("\n无法定位新晶圆，终止自检")
        return 1
    wafer_id = int(match.group(1))
    check("上传后跳到详情页", location.endswith(f"/wafers/{wafer_id}"), location)

    data = api(browser, f"/api/wafers/{wafer_id}/defects")
    check("可行方案为 2 套", data["scheme_count"] == 2, str(data["scheme_count"]))
    per_step: dict[str, dict[str, dict]] = {}
    for row in data["results"]:
        per_step.setdefault(row["step"], {})[row["defect"]] = row

    expected = {"D101": [4, 4], "D103": [8, 8], "D105": [1, 1]}
    for step, defects in per_step.items():
        if step == "封装":
            continue
        ok = all(defects[d]["coordinate"] == cell for d, cell in expected.items())
        check(f"{step} 缺陷坐标与基准一致", ok)

    pack = per_step["封装"]
    check("封装 D101 得基准坐标 (4,4)", pack["D101"]["coordinate"] == [4, 4])
    check("封装 D102 标为待定", pack["D102"]["status"] == "pending")
    check("封装 D104 标为待定", pack["D104"]["status"] == "pending")

    status, page = browser.get(f"/wafers/{wafer_id}?step=6&did=D102")
    check("页面展示待定", status == 200 and "待定" in page)

    locators = api(browser, f"/api/wafers/{wafer_id}/locators")
    steps_by_seq = {s["seq"]: s for s in locators["steps"]}

    # 1) 撤去沉积的 C1：封装失去跨工序约束，相关查询变为待定
    c1_dep = _find_locator(steps_by_seq[3], "C1")
    status, _ = browser.post_form(
        f"/wafers/{wafer_id}/locators/{c1_dep['id']}/remove", {"step": "3", "did": "D101"}
    )
    check("撤点请求成功", status in (302, 303), f"HTTP {status}")
    data = api(browser, f"/api/wafers/{wafer_id}/defects")
    check("撤点后方案数变为 8", data["scheme_count"] == 8, str(data["scheme_count"]))
    pack = {r["defect"]: r for r in data["results"] if r["step"] == "封装"}
    check("撤点后封装 D101 变为待定", pack["D101"]["status"] == "pending")

    # 2) 补回 C1：恢复 2 套方案
    status, _ = browser.post_form(
        f"/wafers/{wafer_id}/locators",
        {
            "step_id": str(steps_by_seq[3]["id"]),
            "step": "3",
            "did": "D101",
            "pid": "C1",
            "row": "4",
            "col": "5",
        },
    )
    check("补点请求成功", status in (302, 303), f"HTTP {status}")
    locators = api(browser, f"/api/wafers/{wafer_id}/locators")
    steps_by_seq = {s["seq"]: s for s in locators["steps"]}
    c1_pack = _find_locator(steps_by_seq[6], "C1")

    # 3) 更正封装 C1 -> (4,4)：D104 由待定变为确定 (5,5)
    status, _ = browser.post_form(
        f"/wafers/{wafer_id}/locators/{c1_pack['id']}/correct",
        {"step": "6", "did": "D104", "row": "4", "col": "4"},
    )
    check("更正请求成功", status in (302, 303), f"HTTP {status}")
    data = api(browser, f"/api/wafers/{wafer_id}/defects")
    pack = {r["defect"]: r for r in data["results"] if r["step"] == "封装"}
    check("更正后 D104 变为确定 (5,5)", pack["D104"]["coordinate"] == [5, 5])

    # 4) 无解更正必须整体回滚，原定位点与原结果沿用
    before = {r["defect"]: (r["status"], r["coordinate"]) for r in data["results"] if r["step"] == "封装"}
    locators = api(browser, f"/api/wafers/{wafer_id}/locators")
    steps_by_seq = {s["seq"]: s for s in locators["steps"]}
    c1_pack = _find_locator(steps_by_seq[6], "C1")
    status, _ = browser.post_form(
        f"/wafers/{wafer_id}/locators/{c1_pack['id']}/correct",
        {"step": "6", "did": "D104", "row": "1", "col": "1"},
    )
    data = api(browser, f"/api/wafers/{wafer_id}/defects")
    after = {r["defect"]: (r["status"], r["coordinate"]) for r in data["results"] if r["step"] == "封装"}
    check("无解更正后原查询结果不变", before == after)
    check("无解更正后仍为 2 套方案", data["scheme_count"] == 2, str(data["scheme_count"]))
    locators = api(browser, f"/api/wafers/{wafer_id}/locators")
    steps_by_seq = {s["seq"]: s for s in locators["steps"]}
    kept = _find_locator(steps_by_seq[6], "C1")
    check(
        "无解更正后定位点仍为原坐标 (4,4)",
        [kept["row"], kept["col"]] == [4, 4],
        f'{kept["row"]},{kept["col"]}',
    )

    # ---- 同格复查：确认、撤点联动降级、补点后人工重新确认、删除 ----
    # 用一张全新的演示片，避免上一节对定位点的更正影响本节的同格前提。
    status, _body, headers = browser.post_files("/wafers", _review_wafer_fields(), demo_csvs())
    match = re.search(r"/wafers/(\d+)$", headers.get("Location", ""))
    check("复查用演示片建立成功", status in (302, 303) and match is not None, f"HTTP {status}")
    review_id = int(match.group(1))
    locators = api(browser, f"/api/wafers/{review_id}/locators")
    steps_by_seq = {s["seq"]: s for s in locators["steps"]}

    review_page = browser.get(f"/wafers/{review_id}")[1]
    check("复查页含同格复查面板", "同格复查" in review_page)

    # 1) 光刻 D101 ⇄ 刻蚀 D101：每套方案都同格 -> 确认
    status, _ = browser.post_form(
        f"/wafers/{review_id}/pairs",
        {
            "pair_step_a": str(steps_by_seq[1]["id"]),
            "pair_defect_a": "D101",
            "pair_step_b": str(steps_by_seq[2]["id"]),
            "pair_defect_b": "D101",
        },
    )
    check("同格提交重定向回复查页", status in (302, 303), f"HTTP {status}")
    pairs = api(browser, f"/api/wafers/{review_id}/pairs")["pairs"]
    check("同格对已确认", len(pairs) == 1 and pairs[0]["status"] == "confirmed")
    check("即时判定为每套方案同格", pairs[0]["live_verdict"] == "same_cell")
    confirmed_id = pairs[0]["id"]

    # 2) 交换填写顺序再交一次：同一对只保留一条
    browser.post_form(
        f"/wafers/{review_id}/pairs",
        {
            "pair_step_a": str(steps_by_seq[2]["id"]),
            "pair_defect_a": "D101",
            "pair_step_b": str(steps_by_seq[1]["id"]),
            "pair_defect_b": "D101",
        },
    )
    pairs = api(browser, f"/api/wafers/{review_id}/pairs")["pairs"]
    check("交换顺序不产生重复记录", len(pairs) == 1 and pairs[0]["id"] == confirmed_id)

    # 3) 光刻 D101 ⇄ 封装 D101：当前也同格，确认后用撤点演示降级
    browser.post_form(
        f"/wafers/{review_id}/pairs",
        {
            "pair_step_a": str(steps_by_seq[1]["id"]),
            "pair_defect_a": "D101",
            "pair_step_b": str(steps_by_seq[6]["id"]),
            "pair_defect_b": "D101",
        },
    )
    pairs = api(browser, f"/api/wafers/{review_id}/pairs")["pairs"]
    packing_pair = next(p for p in pairs if p["step_b"] == "封装")
    check("封装同格对已确认", packing_pair["status"] == "confirmed")

    # 4) 不满足同格的一对 -> 待复核
    status, page = browser.post_form_follow(
        f"/wafers/{review_id}/pairs",
        {
            "pair_step_a": str(steps_by_seq[1]["id"]),
            "pair_defect_a": "D101",
            "pair_step_b": str(steps_by_seq[6]["id"]),
            "pair_defect_b": "D102",
        },
    )
    check("不同格提交登记为待复核", "待复核" in page)
    pairs = api(browser, f"/api/wafers/{review_id}/pairs")["pairs"]
    pending_pair = next(p for p in pairs if p["defect_b"] == "D102" and p["step_b"] == "封装")
    check("待复核记录可在接口查询", pending_pair["status"] == "pending")

    # 5) 撤去沉积 C1：封装失去跨工序约束，已确认的封装对降为待复核；
    #    光刻⇄刻蚀那条不受影响
    c1_dep = _find_locator(steps_by_seq[3], "C1")
    status, page = browser.post_form_follow(
        f"/wafers/{review_id}/locators/{c1_dep['id']}/remove", {"step": "3", "did": "D101"}
    )
    check("撤点成功", status == 200, f"HTTP {status}")
    check("撤点提示同格记录转待复核", "转为待复核" in page)
    pairs = api(browser, f"/api/wafers/{review_id}/pairs")["pairs"]
    by_id = {p["id"]: p for p in pairs}
    check("受影响的确认对降为待复核", by_id[packing_pair["id"]]["status"] == "pending")
    check("未受影响的确认对保持确认", by_id[confirmed_id]["status"] == "confirmed")

    # 6) 补回 C1：即使又能判同格，状态也不自动恢复，必须人工重新确认
    status, _ = browser.post_form(
        f"/wafers/{review_id}/locators",
        {
            "step_id": str(steps_by_seq[3]["id"]),
            "step": "3",
            "did": "D101",
            "pid": "C1",
            "row": "4",
            "col": "5",
        },
    )
    check("补点成功", status in (302, 303), f"HTTP {status}")
    pairs = api(browser, f"/api/wafers/{review_id}/pairs")["pairs"]
    target = next(p for p in pairs if p["id"] == packing_pair["id"])
    check("补点后仍为待复核（不自动确认）", target["status"] == "pending")
    check("但即时判定已恢复同格", target["live_verdict"] == "same_cell")
    status, page = browser.post_form_follow(
        f"/wafers/{review_id}/pairs/{packing_pair['id']}/reconfirm", {}
    )
    check("重新确认成功", status == 200 and "已重新确认同格" in page)
    pairs = api(browser, f"/api/wafers/{review_id}/pairs")["pairs"]
    check(
        "重新确认后状态恢复 confirmed",
        next(p for p in pairs if p["id"] == packing_pair["id"])["status"] == "confirmed",
    )

    # 7) 删除复查记录
    status, _ = browser.post_form(
        f"/wafers/{review_id}/pairs/{pending_pair['id']}/delete", {}
    )
    check("删除记录请求成功", status in (302, 303), f"HTTP {status}")
    pairs = api(browser, f"/api/wafers/{review_id}/pairs")["pairs"]
    check("删除后剩余两条记录", len(pairs) == 2)

    # 8) 无效输入：同一道工序 / 不存在的缺陷
    status, page = browser.post_form_follow(
        f"/wafers/{review_id}/pairs",
        {
            "pair_step_a": str(steps_by_seq[1]["id"]),
            "pair_defect_a": "D101",
            "pair_step_b": str(steps_by_seq[1]["id"]),
            "pair_defect_b": "D102",
        },
    )
    check("同一道工序被拒绝", "两道不同的工序" in page)
    status, page = browser.post_form_follow(
        f"/wafers/{review_id}/pairs",
        {
            "pair_step_a": str(steps_by_seq[1]["id"]),
            "pair_defect_a": "NO-SUCH",
            "pair_step_b": str(steps_by_seq[2]["id"]),
            "pair_defect_b": "D101",
        },
    )
    check("不存在的缺陷被拒绝", "没有缺陷 NO-SUCH" in page)

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过")
    return 1 if failed else 0


def _find_locator(step: dict, pid: str) -> dict:
    for loc in step["locators"]:
        if loc["pid"] == pid:
            return loc
    raise RuntimeError(f"工序 {step['name']} 没有定位点 {pid}")


if __name__ == "__main__":
    sys.exit(main())
