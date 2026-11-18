"""采集API接口：维护API配置，覆盖提交校验、启用配置、停用配置等动作。

本模块同时承载「API配额消耗数据台账」能力：
- /quota/ledger     台账查询（请求编号、调用方、API版本、周期、记账状态）
- /quota/consume    提交消耗（两阶段记账，请求编号幂等）
- /quota/confirm    回执确认（待确认 → 已确认）
- /quota/revoke     撤销未完成请求（待确认 → 已作废，释放预占）
- /quota/balance    按调用方查询完整余额
- /quota/periods    周期版本列表 / 切换当前指针
- /quota/trace      沿采集API调用链溯源
业务失败统一返回 200 + ok=false，避免在页面上产生未处理的错误响应。
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.schemas import ActionPayload, ActionResult, PageResult
from app.services.apigate import ApigateService
from app.services.quota import quota_service

router = APIRouter(prefix="/api/apigate", tags=["采集API"])

service = ApigateService()

LIST_FIELDS = ["配置编号", "端点路径", "请求方法", "鉴权方式", "速率限制", "响应缓存", "调用配额", "版本控制"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


class QuotaConsumePayload(BaseModel):
    values: dict = Field(default_factory=dict)


class QuotaRequestPayload(BaseModel):
    请求编号: str
    备注: str | None = None


class QuotaPeriodPayload(BaseModel):
    周期版本: str


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


# ===================== API 配额消耗数据台账 =====================
@router.get("/quota/ledger", response_model=PageResult[dict])
def list_quota_ledger(
    调用方: str | None = Query(default=None),
    请求编号: str | None = Query(default=None),
    周期版本: str | None = Query(default=None),
    配置编号: str | None = Query(default=None),
    记账状态: str | None = Query(default=None, description="待确认、已确认、已作废"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按调用方、请求编号、周期边界等条件查询台账，分页口径与其他列表一致。"""
    items, total = quota_service.list_ledger(
        caller=调用方, request_no=请求编号, period=周期版本,
        config_code=配置编号, status=记账状态, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/quota/balance")
def get_quota_balance(
    调用方: str = Query(...),
    周期版本: str | None = Query(default=None),
) -> dict:
    """按调用方查询重启前后一致的完整余额；不传周期时取当前周期指针。"""
    return {"ok": True, "item": quota_service.get_balance(调用方, 周期版本)}


@router.get("/quota/periods")
def list_quota_periods() -> dict:
    """列出台账出现过的周期版本及当前指针，供周期边界核对。"""
    return {"ok": True, "current": quota_service.ensure_period(), "items": quota_service.list_periods()}


@router.post("/quota/periods/switch")
def switch_quota_period(payload: QuotaPeriodPayload) -> dict:
    """切换台账当前周期版本；历史台账不动，下游余额/观测指针随之切换。"""
    item, message = quota_service.switch_period(payload.周期版本)
    if item is None:
        return {"ok": False, "message": message}
    return {"ok": True, "message": message, "item": item}


@router.post("/quota/consume")
def submit_quota_consumption(payload: QuotaConsumePayload) -> dict:
    """提交一次配额消耗：请求编号唯一，重复提交只返回既有那一条记账。"""
    values = payload.values or {}
    item, message, duplicated = quota_service.submit_consumption(values)
    if item is None:
        return {"ok": False, "message": message}
    return {"ok": True, "message": message, "duplicated": duplicated, "item": item}


@router.post("/quota/confirm")
def confirm_quota_consumption(payload: QuotaRequestPayload) -> dict:
    """回执确认：待确认转已确认；重启后未完成请求可补确认，重复确认不重复记账。"""
    item, message = quota_service.confirm_consumption(payload.请求编号.strip())
    if item is None:
        return {"ok": False, "message": message}
    return {"ok": True, "message": message, "item": item}


@router.post("/quota/revoke")
def revoke_quota_consumption(payload: QuotaRequestPayload) -> dict:
    """撤销未完成请求：释放预占额度，避免先扣额度再丢回执。"""
    item, message = quota_service.revoke_consumption(payload.请求编号.strip())
    if item is None:
        return {"ok": False, "message": message}
    return {"ok": True, "message": message, "item": item}


@router.get("/quota/recover")
def recover_pending_requests() -> dict:
    """重启恢复视图：列出仍处于待确认的请求，调用方可据此补回执。"""
    items = quota_service.recover_pending()
    return {"ok": True, "total": len(items), "items": items,
            "message": f"重启恢复完成，{len(items)} 个未完成请求恢复为待确认状态"}


@router.get("/quota/trace")
def trace_quota_chain(
    请求编号: str | None = Query(default=None),
    调用方: str | None = Query(default=None),
) -> dict:
    """沿采集API调用链梳理台账来源与接口运行观测、告警待办。"""
    if not 请求编号 and not 调用方:
        return {"ok": False, "message": "请至少提供请求编号或调用方再溯源"}
    return {"ok": True, "item": quota_service.trace_chain(请求编号, 调用方)}


# ===================== 既有 API 配置 CRUD =====================
@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、配置编号关键字与状态过滤采集API列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="apigate.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条API配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"API配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条API配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="API配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条API配置执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
