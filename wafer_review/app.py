"""Flask 应用工厂与路由。"""
from __future__ import annotations

import os
import sqlite3

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .db import connect, init_db
from .display import mapped_grid, transform_label
from .gridview import build_grid
from .model import NoSolutionError, WaferError, analyze
from .overlay import compare_grid
from .service import (
    MAX_STEPS,
    StepInput,
    add_locator,
    correct_locator,
    create_wafer,
    delete_wafer,
    list_wafers,
    load_wafer,
    remove_locator,
)

DEFAULT_DB = "data/wafer_review.sqlite3"


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("WAFER_SECRET", "dev-secret-change-me"),
        DATABASE=os.environ.get("WAFER_DB", DEFAULT_DB),
        MAX_CONTENT_LENGTH=6 * 1024 * 1024,
        MAX_STEPS=MAX_STEPS,
    )
    if test_config:
        app.config.update(test_config)

    init_db(app.config["DATABASE"])

    @app.context_processor
    def _inject() -> dict:
        return {"max_steps": app.config["MAX_STEPS"]}

    @app.get("/")
    def index():
        with connect(app.config["DATABASE"]) as conn:
            wafers = list_wafers(conn)
        summaries = []
        for wafer in wafers:
            analysis = analyze(wafer)
            summaries.append(
                {
                    "wafer": wafer,
                    "solvable": analysis.solvable,
                    "scheme_count": analysis.feasibility.scheme_count,
                    "defect_total": sum(len(s.defects) for s in wafer.steps),
                }
            )
        return render_template("index.html", summaries=summaries)

    @app.get("/wafers/new")
    def new_wafer():
        return render_template("new.html", step_count=3)

    @app.post("/wafers/demo")
    def create_demo():
        from .demo import demo_samples

        steps = [StepInput(name, text) for name, text in demo_samples()]
        try:
            wafer_id = create_wafer(app.config["DATABASE"], "演示晶圆 LOT-2407-A", 8, steps)
        except (WaferError, OSError, sqlite3.Error) as exc:
            flash(f"演示数据建立失败：{exc}", "error")
            return redirect(url_for("index"))
        flash("演示晶圆已建立：6 道工序、8×8 网格", "ok")
        return redirect(url_for("wafer_detail", wafer_id=wafer_id))

    @app.get("/samples/<name>.csv")
    def sample_csv(name: str):
        from .demo import demo_samples

        for index, (step_name, text) in enumerate(demo_samples(), start=1):
            if name in (str(index), step_name, f"{index}_{step_name}"):
                return app.response_class(
                    text,
                    mimetype="text/csv; charset=utf-8",
                    headers={
                        "Content-Disposition": f'inline; filename="{index}_{step_name}.csv"'
                    },
                )
        abort(404)

    @app.post("/wafers")
    def create():
        name = request.form.get("name", "")
        grid_text = request.form.get("grid_size", "")
        try:
            grid_size = int(grid_text)
        except ValueError:
            flash("每边格数必须是整数", "error")
            return redirect(url_for("new_wafer"))
        if not 2 <= grid_size <= 60:
            flash("每边格数必须在 2 到 60 之间", "error")
            return redirect(url_for("new_wafer"))

        step_inputs: list[StepInput] = []
        for i in range(1, app.config["MAX_STEPS"] + 1):
            uploaded = request.files.get(f"csv_{i}")
            text = request.form.get(f"csv_text_{i}", "")
            step_name = request.form.get(f"name_{i}", "").strip()
            if uploaded is not None and uploaded.filename:
                try:
                    text = uploaded.read().decode("utf-8-sig")
                except UnicodeDecodeError:
                    flash(f"第 {i} 个 CSV 不是 UTF-8 编码", "error")
                    return redirect(url_for("new_wafer"))
            if text.strip():
                step_inputs.append(StepInput(step_name or f"工序{i}", text))

        if not step_inputs:
            flash("至少上传一道工序的 CSV", "error")
            return redirect(url_for("new_wafer"))

        try:
            wafer_id = create_wafer(app.config["DATABASE"], name, grid_size, step_inputs)
        except NoSolutionError as exc:
            flash(str(exc), "error")
            return redirect(url_for("new_wafer"))
        except WaferError as exc:
            flash(str(exc), "error")
            return redirect(url_for("new_wafer"))
        except (OSError, sqlite3.Error) as exc:
            flash(f"保存失败：{exc}；原定位点和原查询结果未改动", "error")
            return redirect(url_for("new_wafer"))

        flash("晶圆已建立，定位点与查询结果已生效", "ok")
        return redirect(url_for("wafer_detail", wafer_id=wafer_id))

    @app.get("/wafers/<int:wafer_id>")
    def wafer_detail(wafer_id: int):
        with connect(app.config["DATABASE"]) as conn:
            wafer = load_wafer(conn, wafer_id)
        if wafer is None:
            abort(404)
        analysis = analyze(wafer)
        step_id_text = request.args.get("step")
        did = request.args.get("did", "").strip()
        selected = None
        if step_id_text:
            try:
                selected = wafer.step_by_id(int(step_id_text))
            except ValueError:
                selected = None
        if selected is None and wafer.steps:
            selected = wafer.steps[0]

        results = analysis.step_results(selected) if selected else []
        focus = next((r for r in results if r.did == did), None) if did else None

        grid = build_grid(selected, wafer.grid_size) if selected else None
        mapped = None
        mapped_label = ""
        if selected:
            transforms = analysis.transforms(selected)
            if transforms:
                mapped = mapped_grid(selected, transforms[0], wafer.grid_size)
                mapped_label = transform_label(transforms[0])

        return render_template(
            "wafer.html",
            wafer=wafer,
            feasibility=analysis.feasibility,
            selected=selected,
            results=results,
            focus=focus,
            queried_did=did,
            grid=grid,
            mapped=mapped,
            mapped_label=mapped_label,
            overlay=compare_grid(analysis),
        )

    def _back(wafer_id: int, step: str, did: str):
        params = {"wafer_id": wafer_id}
        if step:
            params["step"] = step
        if did:
            params["did"] = did
        return redirect(url_for("wafer_detail", **params))

    def _apply(
        action,
        success: str,
        failure_prefix: str,
        wafer_id: int,
        step: str,
        did: str,
    ):
        try:
            action()
        except NoSolutionError as exc:
            flash(f"未生效：{exc}", "error")
        except (WaferError, OSError, sqlite3.Error) as exc:
            flash(f"{failure_prefix}：{exc}；已沿用原定位点和原结果", "error")
        else:
            flash(success, "ok")
        return _back(wafer_id, step, did)

    @app.post("/wafers/<int:wafer_id>/locators/<int:locator_id>/correct")
    def correct(wafer_id: int, locator_id: int):
        step = request.form.get("step", "")
        did = request.form.get("did", "")
        try:
            row = int(request.form.get("row", ""))
            col = int(request.form.get("col", ""))
        except ValueError:
            flash("未生效：行列必须是整数；已沿用原定位点和原结果", "error")
            return _back(wafer_id, step, did)
        return _apply(
            lambda: correct_locator(app.config["DATABASE"], wafer_id, locator_id, row, col),
            "定位点已更正，受影响的查询结果已一并更新",
            "更正失败",
            wafer_id,
            step,
            did,
        )

    @app.post("/wafers/<int:wafer_id>/locators/<int:locator_id>/remove")
    def remove(wafer_id: int, locator_id: int):
        step = request.form.get("step", "")
        did = request.form.get("did", "")
        return _apply(
            lambda: remove_locator(app.config["DATABASE"], wafer_id, locator_id),
            "定位点已撤去，受影响的查询结果已一并更新",
            "撤点失败",
            wafer_id,
            step,
            did,
        )

    @app.post("/wafers/<int:wafer_id>/locators")
    def add(wafer_id: int):
        step = request.form.get("step", "")
        did = request.form.get("did", "")
        try:
            step_id = int(request.form.get("step_id", ""))
            row = int(request.form.get("row", ""))
            col = int(request.form.get("col", ""))
        except ValueError:
            flash("未生效：行列必须是整数；已沿用原定位点和原结果", "error")
            return _back(wafer_id, step, did)
        pid = request.form.get("pid", "")
        return _apply(
            lambda: add_locator(app.config["DATABASE"], wafer_id, step_id, pid, row, col),
            "定位点已补充，受影响的查询结果已一并更新",
            "补点失败",
            wafer_id,
            step,
            did,
        )

    @app.post("/wafers/<int:wafer_id>/delete")
    def delete(wafer_id: int):
        delete_wafer(app.config["DATABASE"], wafer_id)
        flash("晶圆记录已删除", "ok")
        return redirect(url_for("index"))

    @app.get("/api/wafers/<int:wafer_id>/defects")
    def api_defects(wafer_id: int):
        with connect(app.config["DATABASE"]) as conn:
            wafer = load_wafer(conn, wafer_id)
        if wafer is None:
            abort(404)
        analysis = analyze(wafer)
        out = []
        for step in wafer.steps:
            for result in analysis.step_results(step):
                out.append(
                    {
                        "step": step.name,
                        "defect": result.did,
                        "source": [result.source_row, result.source_col],
                        "status": result.status,
                        "coordinate": list(result.coordinate) if result.coordinate else None,
                        "candidates": [list(c) for c in result.candidates],
                    }
                )
        return {
            "wafer": wafer.name,
            "grid_size": wafer.grid_size,
            "scheme_count": analysis.feasibility.scheme_count,
            "results": out,
        }

    @app.get("/healthz")
    def healthz():
        with connect(app.config["DATABASE"]) as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/api/wafers/<int:wafer_id>/locators")
    def api_locators(wafer_id: int):
        with connect(app.config["DATABASE"]) as conn:
            wafer = load_wafer(conn, wafer_id)
        if wafer is None:
            abort(404)
        analysis = analyze(wafer)
        return {
            "wafer": wafer.name,
            "revision": wafer.revision,
            "scheme_count": analysis.feasibility.scheme_count,
            "steps": [
                {
                    "id": step.id,
                    "seq": step.seq,
                    "name": step.name,
                    "transforms": list(f.labels) if f else [],
                    "locators": [
                        {"id": loc.id, "pid": loc.pid, "row": loc.row, "col": loc.col}
                        for loc in step.locators
                    ],
                }
                for step, f in (
                    (step, analysis.feasibility.for_step(step.id)) for step in wafer.steps
                )
            ],
        }

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template("404.html"), 404

    return app
