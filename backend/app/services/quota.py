"""API 配额消耗数据台账业务规则。

核心口径：
- 每条记账携带 请求编号 / 调用方 / API版本，请求编号全局唯一，重复提交只保留一条；
- 两阶段记账：submit 先写「待确认」预占额度，confirm 后才转为「已确认」真正消耗；
  重启后未完成请求恢复为待确认，不会出现「先扣额度再丢回执」；
- 已确认消耗 + 待确认预占都参与余量计算，调用方看到的是一个完整余额；
- 周期版本切换只移动当前指针，历史台账不动，余额按调用方+周期聚合；
- 运行监控只从台账取数，不用内存临时计数；同一（周期+配置+调用方）只回写一条告警待办。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.persistence import ledger_store
from app.store import store

MODULE = "apigate"
PENDING = "待确认"
CONFIRMED = "已确认"
VOIDED = "已作废"
DEFAULT_QUOTA = 1000
DEFAULT_API_VERSION = "v1"


def _now_text() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _default_period() -> str:
    return datetime.now().strftime("%Y-%m")


def parse_quota(value: Any) -> int:
    """从 API 配置既有字段「调用配额」里解析整数额度；解析不到给稳定默认值。

    种子/演示数据里该字段可能是“采集API样例1”这类占位文本：数字前出现中文时
    不把末尾编号误当额度，回退默认上限；“1000次/周期”这类真实口径正常取 1000。
    """
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").replace(",", "")
    match = re.search(r"\d+", text)
    if not match:
        return DEFAULT_QUOTA
    if re.search(r"[一-鿿]", text[:match.start()]):
        return DEFAULT_QUOTA
    return int(match.group())


def period_boundary(period: str) -> dict[str, str]:
    """周期边界：按月周期推导起止日期，非标准周期给回退口径，保证调用方能核对。"""
    match = re.fullmatch(r"(\d{4})-(\d{2})", period or "")
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)
        return {"周期版本": period, "周期开始": start.strftime("%Y-%m-%d"), "周期结束": end.strftime("%Y-%m-%d")}
    return {"周期版本": period, "周期开始": f"{period}-01", "周期结束": f"{period}-末"}


class QuotaService:
    # ---------- 基础读取 ----------
    def _find_config_by_code(self, config_code: str | None) -> dict[str, Any] | None:
        if not config_code:
            return None
        for row in store.rows(MODULE):
            if str(row.get("配置编号", "")) == str(config_code):
                return row
        return None

    def _resolve_config(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        config_id = payload.get("配置ID")
        if config_id not in (None, ""):
            entry = store.find(MODULE, int(config_id))
            if entry is None:
                return None, f"API配置 {config_id} 不存在或已归档"
            return entry, None
        entry = self._find_config_by_code(str(payload.get("配置编号") or "").strip() or None)
        if entry is None and not payload.get("配置编号"):
            enabled = [r for r in store.rows(MODULE) if r.get("status") == "已启用"]
            if enabled:
                return enabled[0], None
        if entry is None:
            return None, f"配置编号 {payload.get('配置编号')} 不存在，无法记账"
        return entry, None

    def ensure_period(self) -> str:
        period = ledger_store.current_period()
        if not period:
            period = _default_period()
            ledger_store.set_current_period(period)
        return period

    # ---------- 台账查询 ----------
    def list_ledger(
        self,
        *,
        caller: str | None = None,
        request_no: str | None = None,
        period: str | None = None,
        config_code: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = ledger_store.list_ledger()
        if caller:
            rows = [r for r in rows if caller.casefold() in str(r.get("调用方", "")).casefold()]
        if request_no:
            rows = [r for r in rows if request_no.casefold() in str(r.get("请求编号", "")).casefold()]
        if period:
            rows = [r for r in rows if r.get("周期版本") == period]
        if config_code:
            rows = [r for r in rows if str(r.get("配置编号", "")) == str(config_code)]
        if status:
            rows = [r for r in rows if r.get("记账状态") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_by_request(self, request_no: str) -> dict[str, Any] | None:
        return ledger_store.find_ledger_by_request(request_no)

    # ---------- 两阶段记账 ----------
    def submit_consumption(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        """返回（记账记录, 提示信息, 是否重复提交）。重复请求编号只返回既有那一条。"""
        request_no = str(payload.get("请求编号") or "").strip()
        caller = str(payload.get("调用方") or "").strip()
        if not request_no:
            return None, "缺少请求编号，无法写入配额消耗台账", False
        if not caller:
            return None, "缺少调用方，无法写入配额消耗台账", False
        try:
            amount = int(payload.get("消耗额度") or 1)
        except (TypeError, ValueError):
            return None, "消耗额度必须是整数", False
        if amount <= 0:
            return None, "消耗额度必须大于0", False

        with ledger_store.lock:
            # 幂等：同一请求编号在任何周期都只能出现一条记账记录。
            existing = ledger_store.find_ledger_by_request(request_no)
            if existing is not None:
                return existing, f"请求编号 {request_no} 已记账，重复提交未重复写入", True

            config, error = self._resolve_config(payload)
            if error:
                return None, error, False
            assert config is not None
            period = str(payload.get("周期版本") or "").strip() or self.ensure_period()
            api_version = str(payload.get("API版本") or config.get("版本控制") or DEFAULT_API_VERSION).strip()

            balance = self._balance_locked(caller, period, str(config.get("配置编号")))
            if amount > balance["剩余额度"]:
                return None, (
                    f"调用方 {caller} 在周期 {period} 配额不足："
                    f"上限 {balance['配额上限']}，已确认 {balance['已确认消耗']}，"
                    f"待确认 {balance['待确认消耗']}，剩余 {balance['剩余额度']}"
                ), False

            record = {
                "id": ledger_store.next_ledger_id(),
                "请求编号": request_no,
                "调用方": caller,
                "API版本": api_version,
                "配置编号": config.get("配置编号"),
                "配置ID": config.get("id"),
                "端点路径": config.get("端点路径"),
                "周期版本": period,
                "周期边界": period_boundary(period),
                "消耗额度": amount,
                "记账状态": PENDING,
                "超限标记": False,
                "记账时间": _now_text(),
                "确认时间": None,
                "备注": payload.get("备注"),
            }
            saved = ledger_store.append_ledger_record(record)
            return saved, f"请求 {request_no} 已写入台账并预占额度，等待确认回执", False

    def confirm_consumption(self, request_no: str) -> tuple[dict[str, Any] | None, str]:
        with ledger_store.lock:
            record = ledger_store.find_ledger_by_request(request_no)
            if record is None:
                return None, f"请求编号 {request_no} 在台账中不存在"
            if record["记账状态"] == CONFIRMED:
                # 重复确认也是幂等的：不产生第二条记录。
                return record, f"请求编号 {request_no} 已确认，重复回执未重复记账"
            if record["记账状态"] == VOIDED:
                return None, f"请求编号 {request_no} 已作废，不能再确认"
            updated = ledger_store.update_ledger_record(
                record["id"], {"记账状态": CONFIRMED, "确认时间": _now_text()})
            return updated, f"请求 {request_no} 已确认，额度正式消耗"

    def revoke_consumption(self, request_no: str) -> tuple[dict[str, Any] | None, str]:
        """未完成请求的回执超时/撤销：待确认转作废并释放预占，不扣真实额度。"""
        with ledger_store.lock:
            record = ledger_store.find_ledger_by_request(request_no)
            if record is None:
                return None, f"请求编号 {request_no} 在台账中不存在"
            if record["记账状态"] == VOIDED:
                return record, f"请求编号 {request_no} 已处于作废状态"
            if record["记账状态"] == CONFIRMED:
                return None, f"请求编号 {request_no} 已确认消耗，不能作废"
            updated = ledger_store.update_ledger_record(
                record["id"], {"记账状态": VOIDED, "确认时间": _now_text()})
            return updated, f"请求 {request_no} 已作废，预占额度已释放"

    def recover_pending(self) -> list[dict[str, Any]]:
        """重启恢复：未完成请求保持/恢复为待确认，等调用方补回执，绝不先扣后丢。"""
        recovered: list[dict[str, Any]] = []
        with ledger_store.lock:
            for record in ledger_store.list_ledger():
                if record.get("记账状态") == PENDING:
                    recovered.append(record)
        return recovered

    # ---------- 余额 ----------
    def _aggregate_locked(self, caller: str, period: str, config_code: str | None = None) -> list[dict[str, Any]]:
        rows = [
            r for r in ledger_store.list_ledger()
            if r.get("调用方") == caller and r.get("周期版本") == period
            and (config_code is None or str(r.get("配置编号", "")) == str(config_code))
        ]
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            code = str(row.get("配置编号", ""))
            bucket = groups.setdefault(code, {
                "配置编号": code, "配置ID": row.get("配置ID"), "已确认": 0, "待确认": 0})
            if row.get("记账状态") == CONFIRMED:
                bucket["已确认"] += int(row.get("消耗额度", 0))
            elif row.get("记账状态") == PENDING:
                bucket["待确认"] += int(row.get("消耗额度", 0))
            # 已作废不参与任何额度口径
        return list(groups.values())

    def _balance_locked(self, caller: str, period: str, config_code: str | None = None) -> dict[str, Any]:
        groups = self._aggregate_locked(caller, period, config_code)
        config = self._find_config_by_code(config_code)
        limit = parse_quota(config.get("调用配额")) if config else DEFAULT_QUOTA
        confirmed = sum(g["已确认"] for g in groups)
        pending = sum(g["待确认"] for g in groups)
        used = confirmed + pending
        return {
            "调用方": caller,
            "周期版本": period,
            "周期边界": period_boundary(period),
            "配额上限": limit,
            "已确认消耗": confirmed,
            "待确认消耗": pending,
            "已占用额度": used,
            "剩余额度": limit - used,
            "是否超限": used >= limit,
            "明细": groups,
        }

    def get_balance(self, caller: str, period: str | None = None) -> dict[str, Any]:
        """按调用方查询余额：跨配置聚合成一个完整余额结果，重启前后口径一致。"""
        period = period or self.ensure_period()
        with ledger_store.lock:
            return self._balance_locked(caller, period)

    # ---------- 周期 ----------
    def list_periods(self) -> list[dict[str, Any]]:
        rows = ledger_store.list_ledger()
        versions = sorted({r.get("周期版本") for r in rows if r.get("周期版本")})
        current = self.ensure_period()
        if current not in versions:
            versions.append(current)
            versions.sort()
        result = []
        for version in versions:
            boundary = period_boundary(version)
            result.append({
                "周期版本": version,
                "周期开始": boundary["周期开始"],
                "周期结束": boundary["周期结束"],
                "是否当前周期": version == current,
                "记账条数": sum(1 for r in rows if r.get("周期版本") == version),
            })
        return result

    def switch_period(self, period: str) -> tuple[dict[str, Any] | None, str]:
        period = str(period or "").strip()
        if not period:
            return None, "周期版本不能为空"
        with ledger_store.lock:
            previous = ledger_store.current_period()
            ledger_store.set_current_period(period)
        return {
            "原周期版本": previous,
            "当前周期版本": period,
            "周期边界": period_boundary(period),
        }, f"台账当前指针已切换到周期 {period}，历史台账保持不变"

    # ---------- 运行监控：只从台账取数 ----------
    def quota_observations(self, period: str | None = None) -> list[dict[str, Any]]:
        """接口运行观测清单：台账处理结果按 调用方+配置 聚合，供运行监控页面展示。"""
        period = period or self.ensure_period()
        rows = [r for r in ledger_store.list_ledger() if r.get("周期版本") == period]
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row.get("调用方", "")), str(row.get("配置编号", "")))
            item = grouped.setdefault(key, {
                "调用方": key[0],
                "配置编号": key[1],
                "配置ID": row.get("配置ID"),
                "API版本": row.get("API版本"),
                "周期版本": period,
                "周期边界": period_boundary(period),
                "请求总数": 0,
                "已确认笔数": 0,
                "待确认笔数": 0,
                "已作废笔数": 0,
                "已确认消耗": 0,
                "待确认消耗": 0,
                "重复请求拦截数": 0,
            })
            item["请求总数"] += 1
            state = row.get("记账状态")
            if state == CONFIRMED:
                item["已确认笔数"] += 1
                item["已确认消耗"] += int(row.get("消耗额度", 0))
            elif state == PENDING:
                item["待确认笔数"] += 1
                item["待确认消耗"] += int(row.get("消耗额度", 0))
            elif state == VOIDED:
                item["已作废笔数"] += 1
        config_limits = {
            str(row.get("配置编号")): parse_quota(row.get("调用配额")) for row in store.rows(MODULE)
        }
        result = []
        for item in grouped.values():
            limit = config_limits.get(item["配置编号"], DEFAULT_QUOTA)
            used = item["已确认消耗"] + item["待确认消耗"]
            item["配额上限"] = limit
            item["已占用额度"] = used
            item["剩余额度"] = limit - used
            item["超限结论"] = "超限" if used >= limit else "正常"
            item["是否超限"] = used >= limit
            result.append(item)
        result.sort(key=lambda x: (not x["是否超限"], x["调用方"], x["配置编号"]))
        return result

    def scan_quota_alerts(self, period: str | None = None) -> dict[str, Any]:
        """从台账提取超限调用方并回写告警待办；连续扫描只生成一份，重复结论被拒绝。"""
        period = period or self.ensure_period()
        created: list[dict[str, Any]] = []
        deduped: list[dict[str, Any]] = []
        with ledger_store.lock:
            for observation in self.quota_observations(period):
                if not observation["是否超限"]:
                    continue
                idem_key = f"QUOTA|{period}|{observation['配置编号']}|{observation['调用方']}"
                existed = ledger_store.find_alert_by_key(idem_key)
                if existed is not None:
                    deduped.append(existed)
                    continue
                todo = {
                    "id": ledger_store.next_alert_id(),
                    "幂等键": idem_key,
                    "告警类型": "API配额超限",
                    "调用方": observation["调用方"],
                    "配置编号": observation["配置编号"],
                    "周期版本": period,
                    "周期边界": period_boundary(period),
                    "配额上限": observation["配额上限"],
                    "已确认消耗": observation["已确认消耗"],
                    "待确认消耗": observation["待确认消耗"],
                    "已占用额度": observation["已占用额度"],
                    "超限结论": (
                        f"调用方 {observation['调用方']} 在周期 {period} 已占用 "
                        f"{observation['已占用额度']}/{observation['配额上限']}，达到超限阈值"
                    ),
                    "数据来源": "API配额消耗数据台账",
                    "处理状态": "待处理",
                    "告警时间": _now_text(),
                }
                created.append(ledger_store.append_alert_todo(todo))
        return {"周期版本": period, "新增告警": created, "拦截重复告警": deduped,
                "新增数量": len(created), "重复数量": len(deduped)}

    def list_alert_todos(self, caller: str | None = None, period: str | None = None) -> list[dict[str, Any]]:
        rows = ledger_store.list_alert_todos()
        if caller:
            rows = [r for r in rows if caller.casefold() in str(r.get("调用方", "")).casefold()]
        if period:
            rows = [r for r in rows if r.get("周期版本") == period]
        return rows

    # ---------- 调用链溯源 ----------
    def trace_chain(self, request_no: str | None = None, caller: str | None = None) -> dict[str, Any]:
        """沿采集API调用链梳理：API配置来源 → 台账记账 → 运行监控观测 → 告警待办。"""
        ledger_rows = ledger_store.list_ledger()
        if request_no:
            ledger_rows = [r for r in ledger_rows if r.get("请求编号") == request_no]
        if caller:
            ledger_rows = [r for r in ledger_rows if r.get("调用方") == caller]
        config_codes = {r.get("配置编号") for r in ledger_rows}
        configs = [
            {"配置ID": r.get("id"), "配置编号": r.get("配置编号"), "端点路径": r.get("端点路径"),
             "请求方法": r.get("请求方法"), "版本控制": r.get("版本控制"),
             "调用配额": r.get("调用配额"), "status": r.get("status")}
            for r in store.rows(MODULE) if r.get("配置编号") in config_codes
        ]
        alert_rows = [
            t for t in ledger_store.list_alert_todos()
            if (not caller or t.get("调用方") == caller)
            and t.get("配置编号") in config_codes
        ]
        periods = sorted({r.get("周期版本") for r in ledger_rows if r.get("周期版本")})
        observations: list[dict[str, Any]] = []
        for period in periods:
            for item in self.quota_observations(period):
                if (not caller or item["调用方"] == caller) and item["配置编号"] in config_codes:
                    observations.append(item)
        return {
            "请求编号": request_no,
            "调用方": caller,
            "当前周期版本": self.ensure_period(),
            "API配置来源": configs,
            "台账记账记录": ledger_rows,
            "接口运行观测": observations,
            "告警待办": alert_rows,
        }


quota_service = QuotaService()
