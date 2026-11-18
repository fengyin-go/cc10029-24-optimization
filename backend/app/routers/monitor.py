"""运行监控 分析看板接口：汇总指标、参数运行与结果导出。"""
from __future__ import annotations

import csv
import io
import random
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.monitor import MonitorService

router = APIRouter(prefix="/api/monitor", tags=["运行监控"])
service = MonitorService()
LIST_FIELDS = ["大盘编号", "关联任务", "采集速率", "成功率", "平均延迟", "内存占用", "错误次数", "观测时段"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    return {"metrics": [
        {"label": "覆盖数据源", "value": random.randint(12, 45), "trend": random.randint(-12, 25)},
        {"label": "最新指标值", "value": round(random.uniform(0.72, 0.99), 3), "trend": random.randint(-8, 15)},
        {"label": "异常记录", "value": random.randint(0, 8), "trend": -random.randint(1, 10)},
    ]}


@router.get("")
def list_results() -> dict[str, Any]:
    return {"items": service.list_results()}


@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    results = service.run_analysis(payload.values)
    return {"items": results}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.list_results()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=monitor.csv"})


@router.get("/quota/observations")
def quota_observations(周期版本: str | None = Query(default=None)) -> dict[str, Any]:
    """接口运行观测清单：从 API 配额消耗数据台账聚合，不读内存临时计数。"""
    from app.services.quota import quota_service
    period = 周期版本 or quota_service.ensure_period()
    return {"ok": True, "current": period, "items": service.quota_observations(period)}


@router.post("/quota/scan-alerts")
def scan_quota_alerts(payload: dict[str, object] | None = None) -> dict[str, Any]:
    """扫描超限调用方并回写告警待办；连续运行同一份结论只生成一条待办。"""
    period = None
    if payload:
        period = str(payload.get("周期版本") or "").strip() or None
    result = service.scan_quota_alerts(period)
    result["ok"] = True
    result["message"] = (
        f"扫描完成：新增 {result['新增数量']} 条配额超限告警，"
        f"拦截 {result['重复数量']} 条重复结论"
    )
    return result


@router.get("/quota/alert-todos")
def quota_alert_todos(
    调用方: str | None = Query(default=None),
    周期版本: str | None = Query(default=None),
) -> dict[str, Any]:
    """读取已回写的配额超限告警待办。"""
    items = service.quota_alert_todos(caller=调用方, period=周期版本)
    return {"ok": True, "total": len(items), "items": items}


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"监控大盘 {entry_id} 不存在")
    return entry
