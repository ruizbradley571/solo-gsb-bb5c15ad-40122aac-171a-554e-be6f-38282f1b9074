"""校准工位维护封锁：维护窗口登记、查询与撤销。

业务规则
--------
* 维护窗口与预约共用**唯一校准工位**：占用一律按 UTC **半开区间**
  ``[start_at, end_at)`` 保存维护原因——相接但不重叠的两段（如
  ``[09:00,10:00)`` 与 ``[10:00,11:00)``）互不冲突，可首尾相接。
* 登记（``register_block``）在**同一写事务**（``BEGIN IMMEDIATE``）内
  检查现有预约及生效中维护窗口：任一半开重叠即抛
  :class:`MaintenanceConflict`（路由层映射 409，``detail`` 给出冲突 ID
  列表），事务回滚、绝不写入；全部无冲突才在同一事务内原子写入。
* 撤销（``revoke_block``）只把状态从 ``active`` 改为 ``revoked`` 并记录
  撤销时刻与原因，**不删行**，便于追溯；窗口不存在或已撤销返回 404。
* 空档查询（``booking.find_slots``）把生效中维护窗口与预约一并视作占用；
  确认预约与批量改期在各自写事务内经 :func:`find_maintenance_clashes`
  重查封锁；取消预约后的候补补位同样避开生效窗口并记录跳过原因。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Any, Optional

from .crud import ValidationError
from .utils import fmt, to_utc_naive, utcnow_str

# 维护封锁写事务冲突原因码
CONFLICT_MAINTENANCE_OVERLAP = "MAINTENANCE_WINDOW_OVERLAP"
CONFLICT_MAINTENANCE_NOT_FOUND = "MAINTENANCE_NOT_FOUND"

# 维护窗口状态：active 生效中 | revoked 已撤销
MAINTENANCE_ACTIVE = "active"
MAINTENANCE_REVOKED = "revoked"
MAINTENANCE_STATUSES = (MAINTENANCE_ACTIVE, MAINTENANCE_REVOKED)

_BLOCK_FIELDS = (
    "id",
    "start_at",
    "end_at",
    "reason",
    "status",
    "created_at",
    "revoked_at",
    "revoke_reason",
)


class MaintenanceConflict(Exception):
    """维护封锁写事务冲突（路由层按原因码映射为 HTTP 409/404）。"""

    def __init__(self, code: str, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def _block_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in _BLOCK_FIELDS}


def insert_block(
    conn: sqlite3.Connection,
    *,
    start_at: str,
    end_at: str,
    reason: str,
    block_id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> dict[str, Any]:
    """底层写入（供登记事务与演示播种复用），不做任何重叠校验。"""
    if block_id is None:
        block_id = "MW-" + datetime.utcnow().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:12]
    conn.execute(
        """
        INSERT INTO calibration_maintenance_blocks(
            id, start_at, end_at, reason, status, created_at)
        VALUES (:id, :start_at, :end_at, :reason, :status, :created_at)
        """,
        {
            "id": block_id,
            "start_at": start_at,
            "end_at": end_at,
            "reason": reason,
            "status": MAINTENANCE_ACTIVE,
            "created_at": created_at or utcnow_str(),
        },
    )
    row = conn.execute(
        "SELECT * FROM calibration_maintenance_blocks WHERE id=?", (block_id,)
    ).fetchone()
    return _block_dict(row)


def find_maintenance_clashes(
    conn: sqlite3.Connection, start_s: str, end_s: str
) -> list[dict[str, Any]]:
    """与半开区间 ``[start_s, end_s)`` 重叠的**生效中**维护窗口。

    半开重叠判据 ``m.start < end AND m.end > start``：恰在 ``start_s``
    结束或恰在 ``end_s`` 开始的窗口不冲突（首尾相接允许）。已撤销
    （revoked）窗口不再占用工位，不参与判定。
    """
    rows = conn.execute(
        "SELECT * FROM calibration_maintenance_blocks "
        "WHERE status='active' AND start_at < ? AND end_at > ? "
        "ORDER BY start_at, id",
        (end_s, start_s),
    ).fetchall()
    return [_block_dict(r) for r in rows]


def register_block(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    reason: str,
) -> dict[str, Any]:
    """登记维护窗口：在**同一写事务**内检查现有预约及维护窗口后写入。

    调用方必须提供已执行 ``BEGIN IMMEDIATE`` 的连接；本函数只做检查与
    插入，COMMIT/ROLLBACK 由调用方（路由层上下文）负责。与任一现有
    预约或生效中维护窗口半开重叠即抛 :class:`MaintenanceConflict`
    （409，``detail`` 附冲突 ID 列表），事务回滚、绝不写入；首尾相接
    （新窗口起点恰等于既有占用终点，或反之）允许。
    """
    start_naive = to_utc_naive(start)
    end_naive = to_utc_naive(end)
    if start_naive >= end_naive:
        raise ValidationError("维护窗口起点 start 必须严格早于终点 end")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("维护原因 reason 不能为空（登记后随窗口保存）")
    start_s, end_s = fmt(start_naive), fmt(end_naive)

    # 1) 与现有预约的半开重叠（单工位，不分仪器）
    booking_rows = conn.execute(
        "SELECT * FROM calibration_bookings WHERE start_at < ? AND end_at > ? "
        "ORDER BY start_at, id",
        (end_s, start_s),
    ).fetchall()
    if booking_rows:
        conflicts = [
            {
                "booking_id": r["id"],
                "instrument_code": r["instrument_code"],
                "start_at": r["start_at"],
                "end_at": r["end_at"],
            }
            for r in booking_rows
        ]
        raise MaintenanceConflict(
            CONFLICT_MAINTENANCE_OVERLAP,
            f"维护窗口 {start_s} ~ {end_s}（半开区间）与单工位上 "
            f"{len(conflicts)} 条现有预约重叠，登记被拒绝",
            {
                "conflicting_booking_ids": [c["booking_id"] for c in conflicts],
                "conflicting_bookings": conflicts,
            },
        )

    # 2) 与生效中维护窗口的半开重叠
    block_clashes = find_maintenance_clashes(conn, start_s, end_s)
    if block_clashes:
        raise MaintenanceConflict(
            CONFLICT_MAINTENANCE_OVERLAP,
            f"维护窗口 {start_s} ~ {end_s}（半开区间）与 "
            f"{len(block_clashes)} 个生效中维护窗口重叠，登记被拒绝",
            {
                "conflicting_block_ids": [b["id"] for b in block_clashes],
                "conflicting_blocks": block_clashes,
            },
        )

    # 3) 无冲突：与上述重查处于同一写事务，原子写入
    return insert_block(conn, start_at=start_s, end_at=end_s, reason=reason)


def list_blocks(
    conn: sqlite3.Connection, include_revoked: bool = False
) -> list[dict[str, Any]]:
    """维护窗口列表（按起点升序）；默认只返回生效中窗口。"""
    if include_revoked:
        rows = conn.execute(
            "SELECT * FROM calibration_maintenance_blocks ORDER BY start_at, id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM calibration_maintenance_blocks "
            "WHERE status='active' ORDER BY start_at, id"
        ).fetchall()
    return [_block_dict(r) for r in rows]


def revoke_block(
    conn: sqlite3.Connection, block_id: str, revoke_reason: str = ""
) -> dict[str, Any]:
    """撤销维护窗口：在**同一写事务**内把生效中窗口标记为 revoked。

    只改状态不删行（保留起止区间与维护原因供追溯）；窗口不存在或已
    撤销即抛 :class:`MaintenanceConflict`
    （``MAINTENANCE_NOT_FOUND``，路由层映射 404），不会重复撤销。
    调用方必须提供已执行 ``BEGIN IMMEDIATE`` 的连接。
    """
    row = conn.execute(
        "SELECT * FROM calibration_maintenance_blocks WHERE id=?", (block_id,)
    ).fetchone()
    if row is None or row["status"] == MAINTENANCE_REVOKED:
        raise MaintenanceConflict(
            CONFLICT_MAINTENANCE_NOT_FOUND,
            f"维护窗口不存在或已撤销: {block_id}（不重复撤销）",
            {"block_id": block_id},
        )
    now = utcnow_str()
    conn.execute(
        "UPDATE calibration_maintenance_blocks "
        "SET status='revoked', revoked_at=?, revoke_reason=? WHERE id=?",
        (now, (revoke_reason or "").strip() or None, block_id),
    )
    row = conn.execute(
        "SELECT * FROM calibration_maintenance_blocks WHERE id=?", (block_id,)
    ).fetchone()
    return _block_dict(row)
