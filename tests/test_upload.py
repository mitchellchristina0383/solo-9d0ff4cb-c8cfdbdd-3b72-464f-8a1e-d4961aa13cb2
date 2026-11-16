"""上传规则：幂等重传、异内容拒收、倒退拒收、格式错误、整批原子性。"""
import pytest

from app.database import close_conn, get_conn
from app.services import UploadError, upload_readings
from tests.conftest import all_readings, post_csv


BASE = [
    "2026-01-01T00:00:00Z,1,100.0",
    "2026-01-15T00:00:00Z,1,150.0",
    "2026-01-31T00:00:00Z,1,200.0",
]


def test_first_upload_inserts_all(client):
    resp = post_csv(client, BASE)
    assert resp.status_code == 303
    rows = all_readings()
    assert len(rows) == 3
    conn = get_conn()
    batch = conn.execute("SELECT * FROM upload_batches").fetchone()
    close_conn(conn)
    assert batch["status"] == "accepted" and batch["inserted_cnt"] == 3


def test_same_content_retransmission_is_idempotent(client):
    post_csv(client, BASE)
    resp = post_csv(client, BASE, filename="retry.csv")
    assert resp.status_code == 303
    assert len(all_readings()) == 3  # 没有重复入库
    conn = get_conn()
    b = conn.execute(
        "SELECT * FROM upload_batches WHERE filename='retry.csv'").fetchone()
    close_conn(conn)
    assert b["inserted_cnt"] == 0 and b["skipped_cnt"] == 3


def test_partial_retransmission_mixed_new_and_dup(client):
    post_csv(client, BASE)
    mixed = [
        "2026-01-01T00:00:00Z,1,100.0",   # 完全重复 -> 跳过
        "2026-02-15T00:00:00Z,1,260.0",   # 新增
    ]
    post_csv(client, mixed)
    assert len(all_readings()) == 4
    conn = get_conn()
    b = conn.execute(
        "SELECT * FROM upload_batches ORDER BY id DESC LIMIT 1").fetchone()
    close_conn(conn)
    assert b["inserted_cnt"] == 1 and b["skipped_cnt"] == 1


def test_different_content_same_timestamp_rejects_whole_batch(client):
    post_csv(client, BASE)
    bad = [
        "2026-01-01T00:00:00Z,1,999.0",   # 同刻异内容
        "2026-02-15T00:00:00Z,1,260.0",   # 这条也不允许落地
    ]
    resp = post_csv(client, bad)
    assert resp.status_code == 303
    assert "UploadError" in resp.headers["location"]
    # 读数不变，第二条没有偷偷入库
    assert len(all_readings()) == 3
    conn = get_conn()
    rejected = conn.execute(
        "SELECT * FROM upload_batches WHERE status='rejected'").fetchall()
    close_conn(conn)
    assert len(rejected) == 1 and "冲突" in rejected[0]["message"]


def test_regression_within_batch_rejected(client):
    bad = [
        "2026-01-01T00:00:00Z,1,100.0",
        "2026-01-15T00:00:00Z,1,90.0",    # 倒退
    ]
    post_csv(client, bad)
    assert all_readings() == []
    conn = get_conn()
    assert conn.execute(
        "SELECT COUNT(*) c FROM upload_batches WHERE status='rejected'").fetchone()["c"] == 1
    close_conn(conn)


def test_regression_against_existing_readings_rejected(client):
    post_csv(client, BASE)  # 库内 1/31 为 200
    bad = ["2026-02-10T00:00:00Z,1,199.5"]  # 低于前笔
    post_csv(client, bad)
    assert len(all_readings()) == 3  # 未写入


def test_equal_cumulative_is_allowed_and_zero_usage(client):
    # 持平不算倒退（差额为 0）
    rows = BASE + ["2026-02-01T00:00:00Z,1,200.0"]
    post_csv(client, rows)
    assert len(all_readings()) == 4


def test_duplicate_inside_same_file_rejected(client):
    dup = [
        "2026-01-01T00:00:00Z,1,100.0",
        "2026-01-01T00:00:00Z,1,100.0",
    ]
    post_csv(client, dup)
    assert all_readings() == []


def test_missing_timezone_rejected(client):
    bad = ["2026-01-01T00:00:00,1,100.0"]  # 无 Z / 偏移
    post_csv(client, bad)
    assert all_readings() == []


def test_bad_header_rejected(client):
    content = b"time,meter,value\n2026-01-01T00:00:00Z,1,100.0\n"
    resp = client.post("/upload",
                       files={"file": ("x.csv", content, "text/csv")},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert all_readings() == []


def test_service_level_upload_error_rolls_back(db_path):
    conn = get_conn()
    try:
        with pytest.raises(UploadError):
            upload_readings(conn, "x.csv",
                            b"utc_time,period_no,cumulative_kwh\n"
                            b"2026-01-01T00:00:00Z,1,not-a-number\n")
        # 失败事务回滚后连接仍可正常使用，且无数据
        assert conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"] == 0
    finally:
        close_conn(conn)
