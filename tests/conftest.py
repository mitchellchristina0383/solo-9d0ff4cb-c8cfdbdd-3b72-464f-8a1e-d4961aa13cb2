"""pytest 公共夹具：每个测试使用独立的临时 SQLite 库，彻底杜绝用例间串扰。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_TMP = tempfile.mkdtemp(prefix="energy_test_")
# 打开发布故障注入开关，仅自测使用
os.environ["ENABLE_FAULT_INJECTION"] = "1"

from app import config  # noqa: E402
from app.database import close_conn, get_conn, init_db  # noqa: E402
from app.web import app  # noqa: E402

HEADER = "utc_time,period_no,cumulative_kwh"


@pytest.fixture
def db_path(monkeypatch):
    # 每个用例一个全新文件，避免任何删除/inode/WAL 复用导致的状态串扰
    path = os.path.join(_TMP, f"test_{os.getpid()}_{id(monkeypatch)}.db")
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)
    monkeypatch.setattr(config, "DB_PATH", path)
    init_db(path)
    yield path
    # 用例结束统一关闭并清理
    c = get_conn(path)
    close_conn(c)
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)


@pytest.fixture
def client(db_path):
    with TestClient(app) as c:
        yield c


def post_csv(client, lines, filename="batch.csv"):
    content = ("\n".join([HEADER] + lines)).encode("utf-8")
    return client.post(
        "/upload",
        files={"file": (filename, content, "text/csv")},
        follow_redirects=False,
    )


def all_readings():
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT ts, period_no, cumulative FROM readings ORDER BY ts, period_no"
        ).fetchall()
    finally:
        close_conn(conn)


def version_totals(version_id):
    conn = get_conn()
    try:
        return {
            r["usage_month"]: r["total_kwh"]
            for r in conn.execute(
                "SELECT usage_month, total_kwh FROM version_monthly_totals"
                " WHERE version_id = ?", (version_id,))
        }
    finally:
        close_conn(conn)


def version_details(version_id):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM version_details WHERE version_id = ?"
            " ORDER BY end_ts", (version_id,)).fetchall()
    finally:
        close_conn(conn)


def count_versions():
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) c FROM published_versions").fetchone()["c"]
    finally:
        close_conn(conn)
