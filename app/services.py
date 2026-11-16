"""核心业务逻辑：CSV 解析、批次上传、用量差额计算与区间发布。

规则要点
--------
1. 同刻同内容重传：幂等跳过，不重复入库；同刻异内容：整批拒收。
2. 同周期读数倒退（后笔累计值 < 前笔）：整批拒收。
3. 仅相邻两次读数周期号相同时才计算差额，用量归入后笔所在 UTC 月；
   换表跨周期不估算。
4. 一次发布覆盖相邻两个 UTC 月，明细与月合计在同一事务中落为新版本，
   旧版本保持不变；版本绑定发布时刻的读数状态指纹。
"""
from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


class UploadError(Exception):
    """上传业务错误（触发整批拒收）。"""


class PublishError(Exception):
    """发布业务错误（不产生新版本）。"""


# ---------------------------------------------------------------- 数据结构

@dataclass(frozen=True)
class ReadingRow:
    ts: str            # 规范化后的 UTC 时间字符串
    period_no: int
    cumulative: float
    line_no: int       # 原始 CSV 行号，便于报错定位


@dataclass(frozen=True)
class Interval:
    start_ts: str
    end_ts: str
    period_no: int
    start_cumulative: float
    end_cumulative: float
    usage_kwh: float
    usage_month: str   # 后笔所在月 YYYY-MM


# ---------------------------------------------------------------- CSV 解析

_HEADER_ALIASES = {
    "ts": {"ts", "utctime", "utctimestamp", "time", "timestamp",
           "datetime", "readtime", "utc时间", "时间", "时间戳"},
    "period_no": {"periodno", "period", "meterperiod", "periodnumber",
                  "cycle", "cycleno", "周期号", "表计周期号", "周期"},
    "cumulative": {"cumulative", "cumulativekwh", "kwh", "reading",
                   "total", "totalreading", "energy", "累计", "累计kwh",
                   "累计读数", "累计电量"},
}


def _normalize_header(name: str) -> str:
    return "".join(name.strip().lower().split()).replace("_", "").replace("-", "")


def _parse_ts(raw: str, line_no: int) -> str:
    raw = raw.strip()
    if not raw:
        raise UploadError(f"第 {line_no} 行：UTC 时间为空")
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise UploadError(f"第 {line_no} 行：无法解析 UTC 时间 '{raw}'")
    if dt.tzinfo is None:
        # 必须带时区，避免把本地时间误当 UTC
        raise UploadError(f"第 {line_no} 行：时间 '{raw}' 缺少时区信息（需 UTC，如 2026-01-01T00:00:00Z）")
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S")


def parse_csv(content: bytes) -> list[ReadingRow]:
    """解析上传的 CSV；任何格式问题都抛 UploadError（整批拒收）。"""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UploadError("文件不是有效的 UTF-8 编码 CSV")
    reader = csv.reader(io.StringIO(text))
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise UploadError(f"CSV 解析失败：{exc}")
    if not rows:
        raise UploadError("CSV 为空，缺少表头")

    header = [_normalize_header(h) for h in rows[0]]
    col_index: dict[str, int] = {}
    for idx, name in enumerate(header):
        for field, aliases in _HEADER_ALIASES.items():
            if name in {_normalize_header(a) for a in aliases}:
                col_index[field] = idx
    missing = [f for f in ("ts", "period_no", "cumulative") if f not in col_index]
    if missing:
        raise UploadError(
            "CSV 表头缺少必需列（UTC 时间 / 表计周期号 / 累计 kWh），"
            f"实际表头：{', '.join(rows[0])}"
        )

    parsed: list[ReadingRow] = []
    seen: set[tuple[str, int]] = set()
    for offset, raw_row in enumerate(rows[1:], start=2):
        if not raw_row or all(not c.strip() for c in raw_row):
            continue  # 跳过空行
        if len(raw_row) <= max(col_index.values()):
            raise UploadError(f"第 {offset} 行：列数不足")
        ts = _parse_ts(raw_row[col_index["ts"]], offset)
        try:
            period_no = int(str(raw_row[col_index["period_no"]]).strip())
        except (ValueError, TypeError):
            raise UploadError(f"第 {offset} 行：表计周期号必须是整数")
        try:
            cumulative = float(str(raw_row[col_index["cumulative"]]).strip())
        except (ValueError, TypeError):
            raise UploadError(f"第 {offset} 行：累计 kWh 必须是数字")
        key = (ts, period_no)
        if key in seen:
            raise UploadError(f"第 {offset} 行：文件内存在同刻同周期的重复读数（{ts} / 周期 {period_no}）")
        seen.add(key)
        parsed.append(ReadingRow(ts=ts, period_no=period_no,
                                 cumulative=cumulative, line_no=offset))
    if not parsed:
        raise UploadError("CSV 没有任何数据行")
    return parsed


# ---------------------------------------------------------------- 批次上传

def _record_batch(conn: sqlite3.Connection, filename: str, status: str,
                  inserted: int, skipped: int, message: str) -> int:
    cur = conn.execute(
        "INSERT INTO upload_batches(filename, status, inserted_cnt, skipped_cnt, message)"
        " VALUES (?, ?, ?, ?, ?)",
        (filename, status, inserted, skipped, message),
    )
    return int(cur.lastrowid)


def _record_rejected(filename: str, message: str) -> None:
    """拒收批次用独立事务留痕，不影响已提交读数。"""
    from .database import close_conn, get_conn

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _record_batch(conn, filename, "rejected", 0, 0, message)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        close_conn(conn)


def upload_readings(conn: sqlite3.Connection, filename: str,
                    content: bytes) -> dict:
    """整批上传：成功才提交，任何规则冲突都回滚并留拒收记录。"""
    rows = parse_csv(content)  # 解析失败直接抛错（web 层负责留痕）

    conn.execute("BEGIN IMMEDIATE")
    try:
        batch_id = _record_batch(conn, filename, "accepted", 0, 0, "处理中")

        # 已存在读数（同刻同周期唯一），一次性取出做冲突/幂等判断
        existing = {
            (r["ts"], r["period_no"]): r["cumulative"]
            for r in conn.execute("SELECT ts, period_no, cumulative FROM readings")
        }

        to_insert: list[ReadingRow] = []
        skipped = 0
        for row in rows:
            key = (row.ts, row.period_no)
            if key in existing:
                if existing[key] == row.cumulative:
                    skipped += 1  # 同刻同内容重传：幂等跳过
                else:
                    raise UploadError(
                        f"第 {row.line_no} 行：{row.ts}（周期 {row.period_no}）"
                        f"已存在不同累计值 {existing[key]}，与本次 {row.cumulative} 冲突，整批拒收"
                    )
            else:
                to_insert.append(row)
                existing[key] = row.cumulative

        # 同周期单调性校验：合并库内与本批后按时间排序，逐对检查倒退
        all_points = list(existing.items())  # (ts, period_no) -> cumulative
        by_period: dict[int, list[tuple[str, float]]] = {}
        for (ts, period_no), cum in all_points:
            by_period.setdefault(period_no, []).append((ts, cum))

        for period_no, points in by_period.items():
            points.sort()
            for (prev_ts, prev_cum), (cur_ts, cur_cum) in zip(points, points[1:]):
                if cur_cum < prev_cum:
                    raise UploadError(
                        f"周期 {period_no} 读数倒退：{prev_ts} 的 {prev_cum} "
                        f"-> {cur_ts} 的 {cur_cum}，整批拒收"
                    )

        for row in to_insert:
            conn.execute(
                "INSERT INTO readings(ts, period_no, cumulative, batch_id)"
                " VALUES (?, ?, ?, ?)",
                (row.ts, row.period_no, row.cumulative, batch_id),
            )

        inserted = len(to_insert)
        conn.execute(
            "UPDATE upload_batches SET inserted_cnt = ?, skipped_cnt = ?,"
            " message = ? WHERE id = ?",
            (inserted, skipped,
             f"接受 {inserted} 条，跳过重复 {skipped} 条", batch_id),
        )
        conn.commit()
        return {"batch_id": batch_id, "inserted": inserted,
                "skipped": skipped, "status": "accepted"}
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------- 差额计算

def _normalize_points(readings) -> list[tuple[str, int, float]]:
    """把 sqlite3.Row / dict / tuple 统一成可哈希的 (ts, period_no, cumulative)。"""
    points = []
    for r in readings:
        if isinstance(r, sqlite3.Row) or hasattr(r, "keys"):
            points.append((r["ts"], r["period_no"], r["cumulative"]))
        elif isinstance(r, dict):
            points.append((r["ts"], r["period_no"], r["cumulative"]))
        else:
            points.append((r[0], r[1], r[2]))
    return points


def compute_intervals(readings: list[sqlite3.Row | tuple]) -> list[Interval]:
    """按时间排序，仅相邻且同周期的读数计算差额；换表跨周期不估算。"""
    points = sorted(_normalize_points(readings), key=lambda x: x[0])
    intervals: list[Interval] = []
    for prev, cur in zip(points, points[1:]):
        prev_ts, prev_period, prev_cum = prev
        cur_ts, cur_period, cur_cum = cur
        if prev_period != cur_period:
            continue  # 换表跨周期：不估算
        intervals.append(Interval(
            start_ts=prev_ts,
            end_ts=cur_ts,
            period_no=cur_period,
            start_cumulative=prev_cum,
            end_cumulative=cur_cum,
            usage_kwh=cur_cum - prev_cum,
            usage_month=cur_ts[:7],
        ))
    return intervals


def readings_state_hash(readings: list[sqlite3.Row | tuple]) -> str:
    """对全部已提交读数计算规范指纹，用于证明版本基于哪个读数状态。"""
    points = sorted(_normalize_points(readings), key=lambda x: (x[0], x[1]))
    digest = hashlib.sha256()
    for ts, period_no, cum in points:
        digest.update(f"{ts}|{period_no}|{cum}\n".encode("utf-8"))
    return digest.hexdigest()


# ---------------------------------------------------------------- 区间发布

def _validate_month(value: str, label: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m")
    except (ValueError, TypeError):
        raise PublishError(f"{label} 必须是 YYYY-MM 格式的 UTC 月份")
    return value


def publish_two_months(conn: sqlite3.Connection, month1: str, month2: str,
                       fail_before_commit: bool = False) -> dict:
    """发布相邻两个 UTC 月的区间明细与月合计，整体一个事务、一个新版本。"""
    month1 = _validate_month(month1, "起始月")
    month2 = _validate_month(month2, "结束月")
    y1, m1 = (int(x) for x in month1.split("-"))
    y2, m2 = (int(x) for x in month2.split("-"))
    expected_m = m1 + 1
    expected_y = y1
    if expected_m == 13:
        expected_m, expected_y = 1, y1 + 1
    if (y2, m2) != (expected_y, expected_m):
        raise PublishError(f"{month1} 与 {month2} 不是相邻的两个 UTC 月")

    conn.execute("BEGIN IMMEDIATE")
    try:
        readings = list(conn.execute(
            "SELECT ts, period_no, cumulative FROM readings ORDER BY ts"))
        state_hash = readings_state_hash(readings)
        intervals = [
            iv for iv in compute_intervals(readings)
            if iv.usage_month in (month1, month2)
        ]

        cur = conn.execute(
            "INSERT INTO published_versions(month1, month2, state_hash)"
            " VALUES (?, ?, ?)",
            (month1, month2, state_hash),
        )
        version_id = int(cur.lastrowid)

        for iv in intervals:
            conn.execute(
                "INSERT INTO version_details(version_id, usage_month, start_ts,"
                " end_ts, period_no, start_cumulative, end_cumulative, usage_kwh)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (version_id, iv.usage_month, iv.start_ts, iv.end_ts,
                 iv.period_no, iv.start_cumulative, iv.end_cumulative, iv.usage_kwh),
            )

        totals: dict[str, float] = {month1: 0.0, month2: 0.0}
        for iv in intervals:
            totals[iv.usage_month] += iv.usage_kwh
        for month, total in totals.items():
            conn.execute(
                "INSERT INTO version_monthly_totals(version_id, usage_month, total_kwh)"
                " VALUES (?, ?, ?)",
                (version_id, month, total),
            )

        if fail_before_commit:
            # 自测用故障注入：验证回滚后不留任何新版本痕迹
            raise RuntimeError("故障注入：模拟发布提交前失败")

        conn.commit()
        return {"version_id": version_id, "month1": month1, "month2": month2,
                "state_hash": state_hash,
                "detail_count": len(intervals), "totals": totals}
    except Exception:
        conn.rollback()
        raise
