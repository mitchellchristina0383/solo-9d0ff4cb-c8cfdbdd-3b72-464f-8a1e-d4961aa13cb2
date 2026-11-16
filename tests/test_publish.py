"""差额计算与两月发布：跨周期、月界归属、迟到补传、版本不可变。"""
import pytest

from app.database import close_conn, get_conn
from app.services import (
    PublishError,
    compute_intervals,
    publish_two_months,
    readings_state_hash,
)
from tests.conftest import (
    all_readings,
    count_versions,
    post_csv,
    version_details,
    version_totals,
)


def _publish(m1="2026-01", m2="2026-02", fail=False):
    conn = get_conn()
    try:
        return publish_two_months(conn, m1, m2, fail_before_commit=fail)
    finally:
        close_conn(conn)


# 一组跨越 1/2 月界的读数（严格递增，便于后续插入迟到读数）
BASE = [
    "2026-01-01T00:00:00Z,1,100.0",
    "2026-01-15T00:00:00Z,1,150.0",   # +50 -> 1 月
    "2026-01-31T23:00:00Z,1,200.0",   # +50 -> 1 月
    "2026-02-01T01:00:00Z,1,270.0",   # +70 -> 2 月（月界后笔归属）
    "2026-02-15T12:00:00Z,1,300.0",   # +30 -> 2 月
]


def test_intervals_attributed_to_later_reading_month(client):
    post_csv(client, BASE)
    conn = get_conn()
    try:
        intervals = compute_intervals(
            conn.execute("SELECT ts, period_no, cumulative FROM readings").fetchall())
    finally:
        close_conn(conn)
    by_month = {}
    for iv in intervals:
        by_month.setdefault(iv.usage_month, 0.0)
        by_month[iv.usage_month] += iv.usage_kwh
    assert by_month["2026-01"] == pytest.approx(100.0)
    assert by_month["2026-02"] == pytest.approx(100.0)
    # 跨月界那笔：起在 1 月、止在 2 月，整体归后笔所在的 2 月
    cross = [iv for iv in intervals if iv.start_ts.endswith("23:00:00")][0]
    assert cross.usage_month == "2026-02" and cross.usage_kwh == 70.0


def test_publish_creates_version_with_details_and_totals(client):
    post_csv(client, BASE)
    result = _publish()
    assert result["detail_count"] == 4
    totals = version_totals(result["version_id"])
    assert totals["2026-01"] == pytest.approx(100.0)
    assert totals["2026-02"] == pytest.approx(100.0)
    details = version_details(result["version_id"])
    assert len(details) == 4
    assert all(d["period_no"] == 1 for d in details)


def test_non_adjacent_months_rejected(client):
    post_csv(client, BASE)
    conn = get_conn()
    try:
        with pytest.raises(PublishError):
            publish_two_months(conn, "2026-01", "2026-03")
        with pytest.raises(PublishError):
            publish_two_months(conn, "2026-02", "2026-01")  # 顺序反了
        with pytest.raises(PublishError):
            publish_two_months(conn, "2026-01", "bad")
    finally:
        close_conn(conn)
    assert count_versions() == 0


def test_december_to_january_year_rollover(client):
    rows = [
        "2025-12-15T00:00:00Z,1,10.0",
        "2026-01-15T00:00:00Z,1,40.0",
    ]
    post_csv(client, rows)
    result = _publish("2025-12", "2026-01")
    totals = version_totals(result["version_id"])
    assert totals["2025-12"] == 0.0
    assert totals["2026-01"] == pytest.approx(30.0)


def test_meter_change_across_period_not_estimated(client):
    rows = [
        "2026-01-01T00:00:00Z,1,1000.0",
        "2026-01-20T00:00:00Z,1,1100.0",  # +100，同周期
        "2026-02-01T00:00:00Z,2,0.0",     # 换表：新周期，累计归零
        "2026-02-10T00:00:00Z,2,30.0",    # +30，同周期
    ]
    post_csv(client, rows)
    result = _publish()
    details = version_details(result["version_id"])
    # 只有两段同周期差额，1->2 的换表点不产生明细
    assert len(details) == 2
    assert {(d["period_no"], d["usage_kwh"]) for d in details} == {
        (1, 100.0), (2, 30.0)}
    totals = version_totals(result["version_id"])
    assert totals["2026-01"] == pytest.approx(100.0)
    assert totals["2026-02"] == pytest.approx(30.0)


def test_late_reading_near_boundary_changes_both_months(client):
    """月界附近的迟到补传：验证拆分后月份归属，以及同时改变两个月合计。"""
    post_csv(client, BASE)
    v1 = _publish()
    totals_v1 = version_totals(v1["version_id"])
    assert totals_v1["2026-01"] == pytest.approx(100.0)
    assert totals_v1["2026-02"] == pytest.approx(100.0)

    # 迟到补传 1：插在 1/31 23:00 与 2/1 01:00 之间，后笔落在 2 月
    # 原 +70 的跨月差额被拆成 +50 与 +20，2 月合计不变、明细数 +1
    late = ["2026-02-01T00:00:00Z,1,250.0"]
    post_csv(client, late)

    v2 = _publish()
    assert v2["version_id"] != v1["version_id"]
    totals_v2 = version_totals(v2["version_id"])
    assert totals_v2["2026-01"] == pytest.approx(100.0)
    assert totals_v2["2026-02"] == pytest.approx(100.0)
    assert len(version_details(v2["version_id"])) == 5

    # 迟到补传 2：后笔落在 1 月 —— 两个月合计同时改变
    # 23:00(200)->23:30(230) +30 归 1 月；23:30->00:00(250) +20 归 2 月
    late2 = ["2026-01-31T23:30:00Z,1,230.0"]
    post_csv(client, late2)
    v3 = _publish()
    totals_v3 = version_totals(v3["version_id"])
    assert totals_v3["2026-01"] == pytest.approx(130.0)
    assert totals_v3["2026-02"] == pytest.approx(70.0)
    # 总用量守恒：130 + 70 = 300 - 100
    assert totals_v3["2026-01"] + totals_v3["2026-02"] == pytest.approx(200.0)

    # 旧版本纹丝不动
    assert version_totals(v1["version_id"]) == totals_v1
    assert len(version_details(v1["version_id"])) == 4
    assert version_totals(v2["version_id"]) == totals_v2


def test_versions_pin_consistent_reading_state(client):
    """新版本的两个月来自同一读数状态：状态指纹与当时全量读数一致。"""
    post_csv(client, BASE)
    result = _publish()
    conn = get_conn()
    try:
        current_hash = readings_state_hash(
            conn.execute("SELECT ts, period_no, cumulative FROM readings").fetchall())
        stored = conn.execute(
            "SELECT state_hash FROM published_versions WHERE id=?",
            (result["version_id"],)).fetchone()["state_hash"]
    finally:
        close_conn(conn)
    assert stored == current_hash == result["state_hash"]

    # 补传后指纹变化，旧版指纹保持旧值
    post_csv(client, ["2026-03-01T00:00:00Z,1,320.0"])
    conn = get_conn()
    try:
        new_hash = readings_state_hash(
            conn.execute("SELECT ts, period_no, cumulative FROM readings").fetchall())
        old_hash = conn.execute(
            "SELECT state_hash FROM published_versions WHERE id=?",
            (result["version_id"],)).fetchone()["state_hash"]
    finally:
        close_conn(conn)
    assert new_hash != old_hash


def test_publish_failure_leaves_no_version(client):
    post_csv(client, BASE)
    conn = get_conn()
    try:
        with pytest.raises(RuntimeError):
            publish_two_months(conn, "2026-01", "2026-02",
                               fail_before_commit=True)
    finally:
        close_conn(conn)
    assert count_versions() == 0
    # 故障后仍可正常发布
    ok = _publish()
    assert count_versions() == 1
    assert version_totals(ok["version_id"])["2026-01"] == pytest.approx(100.0)


def test_empty_range_publishes_zero_totals(client):
    post_csv(client, ["2026-03-01T00:00:00Z,1,10.0",
                      "2026-03-02T00:00:00Z,1,12.0"])
    result = _publish()  # 1/2 月区间内没有差额
    totals = version_totals(result["version_id"])
    assert totals == {"2026-01": 0.0, "2026-02": 0.0}
    assert version_details(result["version_id"]) == []
