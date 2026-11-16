"""FastAPI 网页路由：上传、读数查看、两月发布、版本与下载。"""
from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import config
from .database import close_conn, get_conn, init_db
from .services import (
    PublishError,
    UploadError,
    compute_intervals,
    publish_two_months,
    readings_state_hash,
    upload_readings,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="园区总表用量发布平台", lifespan=lifespan)
templates = Jinja2Templates(directory=config.BASE_DIR + "/app/templates")


# ---------------------------------------------------------------- 辅助查询

def _dashboard_data(conn):
    stats = {
        "readings": conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"],
        "batches": conn.execute("SELECT COUNT(*) c FROM upload_batches").fetchone()["c"],
        "versions": conn.execute("SELECT COUNT(*) c FROM published_versions").fetchone()["c"],
        "rejected": conn.execute(
            "SELECT COUNT(*) c FROM upload_batches WHERE status='rejected'").fetchone()["c"],
    }
    latest = conn.execute(
        "SELECT * FROM published_versions ORDER BY id DESC LIMIT 1").fetchone()
    recent_batches = conn.execute(
        "SELECT * FROM upload_batches ORDER BY id DESC LIMIT 8").fetchall()
    return stats, latest, recent_batches


# ---------------------------------------------------------------- 首页

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    conn = get_conn()
    try:
        stats, latest, recent_batches = _dashboard_data(conn)
        return templates.TemplateResponse(request, "home.html", {
            "stats": stats,
            "latest": latest, "recent_batches": recent_batches,
        })
    finally:
        close_conn(conn)


# ---------------------------------------------------------------- 上传

@app.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    conn = get_conn()
    try:
        batches = conn.execute(
            "SELECT * FROM upload_batches ORDER BY id DESC LIMIT 20").fetchall()
        return templates.TemplateResponse(request, "upload.html", {"batches": batches})
    finally:
        close_conn(conn)


@app.post("/upload")
async def upload_csv(request: Request, file: UploadFile = File(...)):
    content = await file.read()
    filename = file.filename or "未命名文件"
    conn = get_conn()
    try:
        result = upload_readings(conn, filename, content)
        return RedirectResponse(
            f"/upload?ok=1&inserted={result['inserted']}&skipped={result['skipped']}",
            status_code=303)
    except UploadError as exc:
        # 解析/规则拒收：独立留痕，读数保持不变
        from .services import _record_rejected
        try:
            _record_rejected(filename, str(exc)[:500])
        except Exception:
            pass  # 留痕失败不掩盖原始拒收原因
        return RedirectResponse(
            "/upload?err=UploadError&msg=" + quote(str(exc)[:300]),
            status_code=303)
    finally:
        close_conn(conn)


# ---------------------------------------------------------------- 读数

@app.get("/readings", response_class=HTMLResponse)
def readings_page(request: Request):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT r.*, b.filename FROM readings r"
            " JOIN upload_batches b ON b.id = r.batch_id"
            " ORDER BY r.ts, r.period_no").fetchall()
        point_rows = conn.execute(
            "SELECT ts, period_no, cumulative FROM readings").fetchall()
        intervals = compute_intervals(point_rows)
        state_hash = readings_state_hash(point_rows)
        return templates.TemplateResponse(request, "readings.html", {
            "rows": rows,
            "intervals": intervals, "state_hash": state_hash,
        })
    finally:
        close_conn(conn)


# ---------------------------------------------------------------- 发布

@app.get("/publish", response_class=HTMLResponse)
def publish_page(request: Request):
    conn = get_conn()
    try:
        versions = conn.execute(
            "SELECT * FROM published_versions ORDER BY id DESC LIMIT 20").fetchall()
        return templates.TemplateResponse(request, "publish.html", {"versions": versions})
    finally:
        close_conn(conn)


@app.post("/publish")
def publish_form(request: Request, month1: str = Form(...), month2: str = Form(...)):
    fail = bool(config.ENABLE_FAULT_INJECTION and
                request.headers.get("X-Fail-Publish") == "1")
    conn = get_conn()
    try:
        result = publish_two_months(conn, month1.strip(), month2.strip(),
                                    fail_before_commit=fail)
        return RedirectResponse(
            f"/versions/{result['version_id']}?published=1", status_code=303)
    except PublishError as exc:
        versions = conn.execute(
            "SELECT * FROM published_versions ORDER BY id DESC LIMIT 20").fetchall()
        return templates.TemplateResponse(request, "publish.html", {
            "versions": versions,
            "error": str(exc), "month1": month1, "month2": month2,
        }, status_code=400)
    except RuntimeError as exc:
        # 含自测故障注入：事务已回滚，不留新版本，按发布失败反馈页面
        versions = conn.execute(
            "SELECT * FROM published_versions ORDER BY id DESC LIMIT 20").fetchall()
        return templates.TemplateResponse(request, "publish.html", {
            "versions": versions,
            "error": f"发布过程中发生错误，已回滚且未产生新版本：{exc}",
            "month1": month1, "month2": month2,
        }, status_code=500)
    finally:
        close_conn(conn)


# ---------------------------------------------------------------- 版本

def _get_version_or_404(conn, version_id: int):
    version = conn.execute(
        "SELECT * FROM published_versions WHERE id = ?", (version_id,)).fetchone()
    if version is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="版本不存在")
    details = conn.execute(
        "SELECT * FROM version_details WHERE version_id = ?"
        " ORDER BY end_ts, period_no", (version_id,)).fetchall()
    totals = conn.execute(
        "SELECT * FROM version_monthly_totals WHERE version_id = ?"
        " ORDER BY usage_month", (version_id,)).fetchall()
    return version, details, totals


@app.get("/versions", response_class=HTMLResponse)
def versions_page(request: Request):
    conn = get_conn()
    try:
        versions = conn.execute(
            "SELECT * FROM published_versions ORDER BY id DESC").fetchall()
        current_hash = readings_state_hash(
            conn.execute("SELECT ts, period_no, cumulative FROM readings").fetchall())
        return templates.TemplateResponse(request, "versions.html", {
            "versions": versions,
            "current_hash": current_hash,
        })
    finally:
        close_conn(conn)


@app.get("/versions/{version_id}", response_class=HTMLResponse)
def version_detail_page(request: Request, version_id: int):
    conn = get_conn()
    try:
        version, details, totals = _get_version_or_404(conn, version_id)
        current_hash = readings_state_hash(
            conn.execute("SELECT ts, period_no, cumulative FROM readings").fetchall())
        return templates.TemplateResponse(request, "version_detail.html", {
            "version": version, "details": details,
            "totals": totals, "current_hash": current_hash,
        })
    finally:
        close_conn(conn)


def _csv_response(headers: list[str], rows: list[list], filename: str) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    # utf-8-sig BOM，保证 Excel 打开中文/表头不乱码
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/versions/{version_id}/details.csv")
def download_details(version_id: int):
    conn = get_conn()
    try:
        _, details, _ = _get_version_or_404(conn, version_id)
        return _csv_response(
            ["usage_month", "start_ts_utc", "end_ts_utc", "period_no",
             "start_cumulative_kwh", "end_cumulative_kwh", "usage_kwh"],
            [[d["usage_month"], d["start_ts"], d["end_ts"], d["period_no"],
              d["start_cumulative"], d["end_cumulative"], d["usage_kwh"]]
             for d in details],
            f"version_{version_id}_details.csv")
    finally:
        close_conn(conn)


@app.get("/versions/{version_id}/totals.csv")
def download_totals(version_id: int):
    conn = get_conn()
    try:
        _, _, totals = _get_version_or_404(conn, version_id)
        return _csv_response(
            ["usage_month", "total_kwh"],
            [[t["usage_month"], t["total_kwh"]] for t in totals],
            f"version_{version_id}_totals.csv")
    finally:
        close_conn(conn)
