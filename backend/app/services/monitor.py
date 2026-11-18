"""运行监控 分析看板服务：汇总指标、运行分析并生成模拟结果。

API 配额相关观测不再使用内存临时计数，统一从「API配额消耗数据台账」提取：
- quota_observations：接口运行观测清单（按调用方+配置聚合的真实记账）；
- scan_quota_alerts：识别超限调用方并幂等回写告警待办，重复扫描不产生重复结果。
"""
from __future__ import annotations

import random
from typing import Any

from app.services.quota import quota_service
from app.store import store

MODULE = "monitor"
REQUIRED_FIELDS = ["大盘编号", "关联任务", "采集速率"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


class MonitorService:
    def list_results(self) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        results: list[dict] = []
        for row in rows:
            d = dict(row)
            d["指标值"] = round(random.uniform(0.65, 0.98), 3)
            d["对比基准"] = round(random.uniform(0.60, 0.92), 3)
            d["差异"] = round(d["指标值"] - d["对比基准"], 3)
            results.append(d)
        return results

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        count = int(params.get("样本范围", params.get("并行数量", "6")))
        results: list[dict] = []
        for i in range(count):
            results.append({
                "id": i + 1,
                "分组": f"分组{(i%3)+1}",
                "指标值": round(random.uniform(0.62, 1.02), 3),
                "对比基准": round(random.uniform(0.58, 0.95), 3),
                "差异": round(random.uniform(-0.2, 0.3), 3),
            })
        return results

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    # ---- API 配额消耗数据台账 → 运行监控 ----
    def quota_observations(self, period: str | None = None) -> list[dict[str, Any]]:
        """接口运行观测清单：数据完全来自持久化台账，刷新/重启后结果一致。"""
        return quota_service.quota_observations(period)

    def scan_quota_alerts(self, period: str | None = None) -> dict[str, Any]:
        """提取异常调用方并把超限结论回写告警待办；重复运行只保留同一份结论。"""
        return quota_service.scan_quota_alerts(period)

    def quota_alert_todos(self, caller: str | None = None, period: str | None = None) -> list[dict[str, Any]]:
        return quota_service.list_alert_todos(caller=caller, period=period)
