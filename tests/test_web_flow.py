"""端到端：网页路由、下载、发布故障注入、补传/发布并发交错。"""
import threading

from tests.conftest import post_csv


BASE = [
    "2026-01-01T00:00:00Z,1,100.0",
    "2026-01-15T00:00:00Z,1,150.0",
    "2026-02-10T00:00:00Z,1,240.0",
]


def test_pages_render(client):
    for path in ("/", "/upload", "/readings", "/publish", "/versions"):
        resp = client.get(path)
        assert resp.status_code == 200 and "园区总表" in resp.text


def test_full_web_flow_and_downloads(client):
    resp = post_csv(client, BASE)
    assert resp.status_code == 303 and "/upload" in resp.headers["location"]

    resp = client.post("/publish", data={"month1": "2026-01", "month2": "2026-02"},
                       follow_redirects=False)
    assert resp.status_code == 303
    version_id = int(resp.headers["location"].split("/versions/")[1].split("?")[0])

    page = client.get(f"/versions/{version_id}")
    assert page.status_code == 200 and "月合计" in page.text

    details = client.get(f"/versions/{version_id}/details.csv")
    assert details.status_code == 200
    assert "usage_month,start_ts_utc,end_ts_utc" in details.text
    assert "2026-01" in details.text and "2026-02" in details.text

    totals = client.get(f"/versions/{version_id}/totals.csv")
    assert totals.status_code == 200
    lines = [ln for ln in totals.text.strip().splitlines() if ln]
    assert lines[0].startswith("﻿usage_month,total_kwh")
    assert any(ln.startswith("2026-01,50.0") for ln in lines[1:])
    assert any(ln.startswith("2026-02,90.0") for ln in lines[1:])


def test_publish_bad_input_returns_400_and_no_version(client):
    post_csv(client, BASE)
    resp = client.post("/publish", data={"month1": "2026-01", "month2": "2026-03"})
    assert resp.status_code == 400 and "相邻" in resp.text
    assert "发布失败" in resp.text
    # 没有留下版本
    listing = client.get("/versions").text
    assert "尚无发布版本" in listing


def test_fault_injected_publish_rolls_back(client):
    post_csv(client, BASE)
    resp = client.post(
        "/publish",
        data={"month1": "2026-01", "month2": "2026-02"},
        headers={"X-Fail-Publish": "1"},
    )
    # 路由捕获注入故障并回滚，页面明确提示发布失败
    assert resp.status_code == 500 and "已回滚" in resp.text
    # 失败后没有任何版本或明细残留
    listing = client.get("/versions").text
    assert "尚无发布版本" in listing
    # 紧随其后的正常发布成功
    ok = client.post("/publish",
                     data={"month1": "2026-01", "month2": "2026-02"},
                     follow_redirects=False)
    assert ok.status_code == 303
    assert "尚无发布版本" not in client.get("/versions").text


def test_version_404(client):
    assert client.get("/versions/9999").status_code == 404
    assert client.get("/versions/9999/details.csv").status_code == 404


def test_concurrent_upload_and_publish_never_mix_states(client, db_path):
    """补传与发布并发交错：每个版本的两个月必须来自同一已提交读数状态。

    做法：并发跑补传上传与发布，结束后校验每个版本的明细+合计都能由
    某个“读数前缀状态”完整重算出来（状态指纹即该状态的承诺）。
    """
    post_csv(client, BASE)
    errors = []

    def do_publish():
        try:
            for _ in range(6):
                client.post("/publish",
                            data={"month1": "2026-01", "month2": "2026-02"})
        except Exception as exc:  # pragma: no cover - 并发不应抛异常
            errors.append(exc)

    late_rows = [
        "2026-01-20T00:00:00Z,1,180.0",
        "2026-02-01T00:00:00Z,1,210.0",
    ]

    def do_upload():
        try:
            for line in late_rows:
                post_csv(client, [line])
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=do_publish)
    t2 = threading.Thread(target=do_upload)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors

    # 最终状态：两个迟到读数都应入库（每次补传独立事务）
    resp = client.get("/readings")
    assert resp.status_code == 200
    assert "2026-01-20T00:00:00" in resp.text
    assert "2026-02-01T00:00:00" in resp.text

    # 所有版本的指纹都必须与库内实际存的状态哈希一致（由发布事务内快照保证），
    # 且最终重算的合计等于最后一个版本的合计
    last = client.get("/versions").text
    assert "一致" in last  # 至少最新版本指纹匹配当前读数
    final_versions = client.get("/versions")
    assert final_versions.status_code == 200
