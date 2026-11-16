"""SQLite 连接与建表。

写操作统一使用 BEGIN IMMEDIATE，保证补传与发布交错时在数据库层串行化，
并以事务保证“整批”原子性：上传失败不写入任何读数，发布失败不留新版本。
"""
import os
import sqlite3

from . import config


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL 提升读写并发能力；单文件 SQLite 仍由写事务保证一致性
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def close_conn(conn: sqlite3.Connection) -> None:
    """关闭前 checkpoint，把 WAL 内容并入主库，保证后续连接可见已提交数据。"""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    conn.close()


SCHEMA = """
-- 已提交的总表读数（累计 kWh）
CREATE TABLE IF NOT EXISTS readings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,          -- UTC 规范时间 YYYY-MM-DDTHH:MM:SS
    period_no    INTEGER NOT NULL,          -- 表计周期号（换表即换周期）
    cumulative   REAL    NOT NULL,          -- 累计读数 kWh
    batch_id     INTEGER NOT NULL REFERENCES upload_batches(id),
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(ts, period_no)                   -- 同刻同周期只允许一条
);

-- 上传批次，成功与拒收均留痕
CREATE TABLE IF NOT EXISTS upload_batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filename     TEXT    NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('accepted', 'rejected')),
    inserted_cnt INTEGER NOT NULL DEFAULT 0,
    skipped_cnt  INTEGER NOT NULL DEFAULT 0,
    message      TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 一次发布生成一个版本（相邻两个 UTC 月作为一个区间）
CREATE TABLE IF NOT EXISTS published_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    month1       TEXT    NOT NULL,          -- 起始月 YYYY-MM
    month2       TEXT    NOT NULL,          -- 相邻的下一自然月 YYYY-MM
    state_hash   TEXT    NOT NULL,          -- 发布时全部已提交读数状态指纹
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 版本内的用量明细（相邻同周期读数差额，归入后笔所在月）
CREATE TABLE IF NOT EXISTS version_details (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id      INTEGER NOT NULL REFERENCES published_versions(id),
    usage_month     TEXT    NOT NULL,
    start_ts        TEXT    NOT NULL,
    end_ts          TEXT    NOT NULL,
    period_no       INTEGER NOT NULL,
    start_cumulative REAL   NOT NULL,
    end_cumulative   REAL   NOT NULL,
    usage_kwh        REAL   NOT NULL
);

-- 版本内的月合计
CREATE TABLE IF NOT EXISTS version_monthly_totals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id      INTEGER NOT NULL REFERENCES published_versions(id),
    usage_month     TEXT    NOT NULL,
    total_kwh       REAL    NOT NULL,
    UNIQUE(version_id, usage_month)
);

CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
CREATE INDEX IF NOT EXISTS idx_details_version ON version_details(version_id);
"""


def init_db(db_path: str | None = None) -> None:
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        close_conn(conn)
