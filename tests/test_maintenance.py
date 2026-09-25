import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TRACE_DB_PATH"] = _tmp.name
os.environ["SEED_DEMO"] = "0"

from app.database import get_conn_ctx, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()

SLOT_START = "2026-06-20T09:00:00Z"


def _chain(client, *, parent_until="2030-01-01T00:00:00",
           leaf_until="2027-01-01T00:00:00",
           leaf_from="2026-01-01T00:00:00",
           code="W"):
    """提交 基准 G -> 参考 R -> 工作仪器 W 的唯一有效链（同 test_calibration）。"""
    cg, cr, cw = f"CG-{code}", f"CR-{code}", f"CW-{code}"
    instruments = [
        {"code": f"G-{code}", "name": f"国家基准-{code}",
         "is_national_standard": True},
        {"code": f"R-{code}", "name": f"参考标准器-{code}"},
        {"code": code, "name": "工作仪器"},
    ]
    certs = [
        {"id": cg, "instrument_code": f"G-{code}", "cert_number": f"{code}-G1",
         "issued_at": "2024-01-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00", "valid_until": "2031-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": cr, "instrument_code": f"R-{code}", "cert_number": f"{code}-R1",
         "issued_at": "2025-06-01T00:00:00",
         "valid_from": "2025-06-01T00:00:00", "valid_until": parent_until,
         "uncertainty": 0.001, "unit": "mm"},
        {"id": cw, "instrument_code": code, "cert_number": f"{code}-W1",
         "issued_at": "2026-01-02T00:00:00",
         "valid_from": leaf_from, "valid_until": leaf_until,
         "uncertainty": 0.01, "unit": "mm"},
    ]
    edges = [
        {"child_cert_id": cw, "parent_cert_id": cr},
        {"child_cert_id": cr, "parent_cert_id": cg},
    ]
    r = client.post("/api/submissions",
                    json={"instruments": instruments, "certificates": certs,
                          "edges": edges})
    assert r.status_code == 201, r.text


def _book(client, code="W", start=SLOT_START, duration=3600, note=""):
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": code, "start": start,
        "duration_seconds": duration, "note": note,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _block(client, start, end, reason="例行维护"):
    r = client.post("/api/calibration-maintenance", json={
        "start": start, "end": end, "reason": reason,
    })
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/api/admin/reset")
        yield c


# ---------------------------------------------------------------- 登记

def test_register_block_created_and_persisted(client):
    blk = _block(client, "2026-06-22T08:00:00Z", "2026-06-22T14:00:00Z",
                 reason="工位导轨例行维护")
    assert blk["id"].startswith("MW-")
    assert blk["start_at"] == "2026-06-22T08:00:00Z"
    assert blk["end_at"] == "2026-06-22T14:00:00Z"
    assert blk["reason"] == "工位导轨例行维护"
    assert blk["status"] == "active"
    assert blk["created_at"] is not None
    assert blk["revoked_at"] is None
    assert blk["revoke_reason"] is None

    # 列表可查（按起点升序）
    rows = client.get("/api/calibration-maintenance").json()
    assert [b["id"] for b in rows] == [blk["id"]]

    # 持久化到 SQLite：另起连接可见
    with get_conn_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM calibration_maintenance_blocks WHERE id=?",
            (blk["id"],),
        ).fetchone()
        assert row is not None
        assert row["status"] == "active"
        assert row["reason"] == "工位导轨例行维护"


def test_register_block_invalid_interval_422(client):
    # 起点 == 终点 / 起点晚于终点：半开区间为空或倒置
    for start, end in [("2026-06-22T08:00:00Z", "2026-06-22T08:00:00Z"),
                       ("2026-06-22T14:00:00Z", "2026-06-22T08:00:00Z")]:
        r = client.post("/api/calibration-maintenance", json={
            "start": start, "end": end, "reason": "检修",
        })
        assert r.status_code == 422
    assert client.get("/api/calibration-maintenance").json() == []


def test_register_block_blank_reason_422(client):
    # 空串被 pydantic 拦截；纯空白在服务层按空原因拒绝
    r = client.post("/api/calibration-maintenance", json={
        "start": "2026-06-22T08:00:00Z", "end": "2026-06-22T09:00:00Z",
        "reason": "",
    })
    assert r.status_code == 422
    r = client.post("/api/calibration-maintenance", json={
        "start": "2026-06-22T08:00:00Z", "end": "2026-06-22T09:00:00Z",
        "reason": "   ",
    })
    assert r.status_code == 422
    assert client.get("/api/calibration-maintenance").json() == []


def test_register_block_overlaps_booking_409(client):
    _chain(client)
    bk = _book(client, start="2026-06-20T09:00:00Z")          # [09,10)
    r = client.post("/api/calibration-maintenance", json={
        "start": "2026-06-20T09:30:00Z", "end": "2026-06-20T10:30:00Z",
        "reason": "临时检修",
    })
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "MAINTENANCE_WINDOW_OVERLAP"
    assert d["detail"]["conflicting_booking_ids"] == [bk["id"]]
    # 失败不写入：维护窗口列表仍为空，预约不受影响
    assert client.get("/api/calibration-maintenance").json() == []
    assert len(client.get("/api/calibration-bookings").json()) == 1


def test_register_block_overlaps_block_409(client):
    blk = _block(client, "2026-06-22T08:00:00Z", "2026-06-22T11:00:00Z")
    r = client.post("/api/calibration-maintenance", json={
        "start": "2026-06-22T10:00:00Z", "end": "2026-06-22T12:00:00Z",
        "reason": "与既有窗口重叠",
    })
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "MAINTENANCE_WINDOW_OVERLAP"
    assert d["detail"]["conflicting_block_ids"] == [blk["id"]]
    # 失败不写入：仍只有原窗口
    rows = client.get("/api/calibration-maintenance").json()
    assert [b["id"] for b in rows] == [blk["id"]]


def test_register_block_back_to_back_allowed(client):
    _chain(client)
    _book(client, start="2026-06-20T09:00:00Z")               # 预约 [09,10)
    # 与预约首尾相接：起点恰等于预约终点
    b1 = _block(client, "2026-06-20T10:00:00Z", "2026-06-20T12:00:00Z")
    # 与预约另一端首尾相接：终点恰等于预约起点
    b2 = _block(client, "2026-06-20T07:00:00Z", "2026-06-20T09:00:00Z")
    # 与维护窗口首尾相接
    b3 = _block(client, "2026-06-20T12:00:00Z", "2026-06-20T13:00:00Z")
    rows = client.get("/api/calibration-maintenance").json()
    assert [b["id"] for b in rows] == [b2["id"], b1["id"], b3["id"]]


def test_concurrent_register_single_success(client):
    """并发登记重叠窗口：写事务串行化，恰一个 201，另一个 409。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)

    def register():
        barrier.wait(timeout=5)
        with TestClient(app) as local:
            return local.post("/api/calibration-maintenance", json={
                "start": "2026-06-22T08:00:00Z",
                "end": "2026-06-22T10:00:00Z",
                "reason": "并发登记",
            }).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: register(), range(2)))

    assert sorted(results) == [201, 409]
    assert len(client.get("/api/calibration-maintenance").json()) == 1


# ---------------------------------------------------------------- 列表与撤销

def test_list_blocks_excludes_revoked_by_default(client):
    b1 = _block(client, "2026-06-22T08:00:00Z", "2026-06-22T10:00:00Z")
    b2 = _block(client, "2026-06-23T08:00:00Z", "2026-06-23T10:00:00Z")
    assert client.delete(
        f"/api/calibration-maintenance/{b1['id']}").status_code == 200

    rows = client.get("/api/calibration-maintenance").json()
    assert [b["id"] for b in rows] == [b2["id"]]

    rows = client.get("/api/calibration-maintenance",
                      params={"include_revoked": "true"}).json()
    assert [b["id"] for b in rows] == [b1["id"], b2["id"]]
    assert rows[0]["status"] == "revoked"
    assert rows[1]["status"] == "active"


def test_revoke_block_marks_status_keeps_row(client):
    blk = _block(client, "2026-06-22T08:00:00Z", "2026-06-22T10:00:00Z",
                 reason="计划维护")
    r = client.delete(f"/api/calibration-maintenance/{blk['id']}",
                      params={"reason": "维护提前完成"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["id"] == blk["id"]
    assert d["status"] == "revoked"
    assert d["revoked_at"] is not None
    assert d["revoke_reason"] == "维护提前完成"
    # 起止区间与维护原因保留供追溯（只改状态不删行）
    assert d["start_at"] == "2026-06-22T08:00:00Z"
    assert d["reason"] == "计划维护"

    with get_conn_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM calibration_maintenance_blocks WHERE id=?",
            (blk["id"],),
        ).fetchone()
        assert row is not None
        assert row["status"] == "revoked"


def test_revoke_unknown_or_repeated_404(client):
    r = client.delete("/api/calibration-maintenance/MW-GHOST")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "MAINTENANCE_NOT_FOUND"

    blk = _block(client, "2026-06-22T08:00:00Z", "2026-06-22T10:00:00Z")
    assert client.delete(
        f"/api/calibration-maintenance/{blk['id']}").status_code == 200
    # 重复撤销：404，不重复处理
    r = client.delete(f"/api/calibration-maintenance/{blk['id']}")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "MAINTENANCE_NOT_FOUND"


# ---------------------------------------------------------------- 空档查询

def test_slot_query_treats_maintenance_as_busy(client):
    _chain(client)
    _book(client, start="2026-06-20T09:00:00Z")               # 预约 [09,10)
    blk = _block(client, "2026-06-20T11:00:00Z", "2026-06-20T12:00:00Z")
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T08:00:00Z",
        "end": "2026-06-20T13:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    # [08,09) 空档 1 小时 -> 最早 08:00；维护窗口与预约一并出现在 busy
    assert d["slot"]["start"] == "2026-06-20T08:00:00Z"
    kinds = {b["kind"] for b in d["busy"]}
    assert kinds == {"booking", "maintenance"}
    maint = [b for b in d["busy"] if b["kind"] == "maintenance"][0]
    assert maint["block_id"] == blk["id"]
    assert maint["reason"] is not None
    assert maint["start_at"] == "2026-06-20T11:00:00Z"
    booking = [b for b in d["busy"] if b["kind"] == "booking"][0]
    assert booking["instrument_code"] == "W"
    assert booking["block_id"] is None


def test_slot_query_blocked_by_maintenance_window(client):
    _chain(client)
    _block(client, "2026-06-20T09:00:00Z", "2026-06-20T12:00:00Z")
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T13:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    # [09,12) 被维护封锁 -> 最早空档被推后到 12:00（半开相接）
    assert d["slot"]["start"] == "2026-06-20T12:00:00Z"
    assert d["slot"]["end"] == "2026-06-20T13:00:00Z"


def test_slot_query_revoked_block_frees_station(client):
    _chain(client)
    blk = _block(client, "2026-06-20T09:00:00Z", "2026-06-20T12:00:00Z")
    assert client.delete(
        f"/api/calibration-maintenance/{blk['id']}").status_code == 200
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T13:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    # 已撤销窗口不再占用：空档从窗口原起点开始，busy 为空
    assert d["slot"]["start"] == "2026-06-20T09:00:00Z"
    assert d["busy"] == []


# ---------------------------------------------------------------- 确认预约

def test_confirm_booking_blocked_by_maintenance_409(client):
    _chain(client)
    blk = _block(client, "2026-06-20T09:00:00Z", "2026-06-20T12:00:00Z")
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W", "start": "2026-06-20T10:00:00Z",
        "duration_seconds": 3600,
    })
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "MAINTENANCE_WINDOW_OVERLAP"
    assert d["detail"]["conflicting_block_ids"] == [blk["id"]]
    # 冲突不写入：无任何预约
    assert client.get("/api/calibration-bookings").json() == []

    # 半开：与维护窗口首尾相接（起点恰等于窗口终点）允许确认
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W", "start": "2026-06-20T12:00:00Z",
        "duration_seconds": 3600,
    })
    assert r.status_code == 201, r.text


def test_confirm_booking_allowed_after_revoke(client):
    _chain(client)
    blk = _block(client, "2026-06-20T09:00:00Z", "2026-06-20T12:00:00Z")
    assert client.delete(
        f"/api/calibration-maintenance/{blk['id']}").status_code == 200
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W", "start": "2026-06-20T10:00:00Z",
        "duration_seconds": 3600,
    })
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------- 批量改期

def test_reschedule_blocked_by_maintenance_rollback(client):
    """改期新时段撞上维护窗口：409 且整批回滚（批次内其他预约也不动）。"""
    _chain(client)
    _chain(client, code="W2")
    bk_a = _book(client, code="W", start="2026-06-20T09:00:00Z")   # [09,10)
    bk_b = _book(client, code="W2", start="2026-06-20T11:00:00Z")  # [11,12)
    blk = _block(client, "2026-06-21T15:00:00Z", "2026-06-21T16:00:00Z")

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": bk_a["id"], "start": "2026-06-21T09:00:00Z"},
            {"booking_id": bk_b["id"], "start": "2026-06-21T15:00:00Z"},
        ],
    })
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "MAINTENANCE_WINDOW_OVERLAP"
    assert d["detail"]["booking_id"] == bk_b["id"]
    assert d["detail"]["conflicting_block_ids"] == [blk["id"]]

    # 整体回滚：两条预约均保持改期前时段
    rows = {b["id"]: b for b in client.get("/api/calibration-bookings").json()}
    assert rows[bk_a["id"]]["start_at"] == "2026-06-20T09:00:00Z"
    assert rows[bk_b["id"]]["start_at"] == "2026-06-20T11:00:00Z"


def test_reschedule_around_maintenance_ok(client):
    """改期到维护窗口之外（含首尾相接）：正常更新。"""
    _chain(client)
    _chain(client, code="W2")
    bk_a = _book(client, code="W", start="2026-06-20T09:00:00Z")
    bk_b = _book(client, code="W2", start="2026-06-20T11:00:00Z")
    _block(client, "2026-06-21T10:00:00Z", "2026-06-21T12:00:00Z")

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            # 终点恰等于窗口起点（半开相接，允许）
            {"booking_id": bk_a["id"], "start": "2026-06-21T09:00:00Z"},
            # 起点恰等于窗口终点（半开相接，允许）
            {"booking_id": bk_b["id"], "start": "2026-06-21T12:00:00Z"},
        ],
    })
    assert r.status_code == 200, r.text
    rows = {b["id"]: b for b in r.json()}
    assert rows[bk_a["id"]]["end_at"] == "2026-06-21T10:00:00Z"
    assert rows[bk_b["id"]]["start_at"] == "2026-06-21T12:00:00Z"


# ---------------------------------------------------------------- 候补补位

def test_waitlist_promotion_skips_maintenance_window(client):
    """补位重验撞上维护窗口：跳过该候补并记录原因，时段真正释放。"""
    from app.maintenance import insert_block

    _chain(client)
    _chain(client, code="W2")
    bk = _book(client, code="W")
    e = client.post(f"/api/calibration-bookings/{bk['id']}/waitlist",
                    json={"instrument_code": "W2"}).json()
    # 直接落库一个覆盖该时段的维护窗口（绕过登记校验，
    # 模拟候补登记之后工位维护计划发生变化）
    with get_conn_ctx() as conn:
        insert_block(
            conn, block_id="MW-SUDDEN",
            start_at="2026-06-20T08:00:00Z", end_at="2026-06-20T11:00:00Z",
            reason="突发维护",
        )

    r = client.delete(f"/api/calibration-bookings/{bk['id']}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["promoted"] is False
    assert d["promotion"] is None
    assert d["skipped"][0]["waitlist_id"] == e["id"]
    assert d["skipped"][0]["reason_code"] == "MAINTENANCE_WINDOW_OVERLAP"

    entry = client.get("/api/calibration-waitlist").json()[0]
    assert entry["status"] == "failed"
    assert "维护" in entry["status_reason"]
    # 无人补位：时段真正释放
    assert client.get("/api/calibration-bookings").json() == []


def test_waitlist_promotion_ok_outside_maintenance(client):
    """候补时段与维护窗口不重叠（首尾相接）：正常补位。"""
    _chain(client)
    _chain(client, code="W2")
    bk = _book(client, code="W")                                # [09,10)
    e = client.post(f"/api/calibration-bookings/{bk['id']}/waitlist",
                    json={"instrument_code": "W2"}).json()
    # 维护窗口 [10,12)：与候补时段 [09,10) 首尾相接，不影响补位
    _block(client, "2026-06-20T10:00:00Z", "2026-06-20T12:00:00Z")

    r = client.delete(f"/api/calibration-bookings/{bk['id']}")
    d = r.json()
    assert d["promoted"] is True
    assert d["promotion"]["waitlist_id"] == e["id"]
    assert d["promotion"]["booking"]["start_at"] == SLOT_START
