"""API 配额消耗数据台账持久化层。

设计目标：
- 配额消耗记录、告警待办与当前周期指针全部落盘，进程重启后从同一文件恢复；
- 所有写操作在同一把可重入锁内完成，并通过「临时文件 + os.replace」原子落盘，
  避免刷新/返回后读到半成品，也保证连续重复提交只会写入一份；
- 只负责存取，不含业务判断；业务规则在 app/services/quota.py。

环境变量 QUOTA_LEDGER_PATH 可覆盖台账文件位置，便于测试隔离。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

_LEDGER_PATH = os.environ.get(
    "QUOTA_LEDGER_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "quota_ledger.json"),
)

# 台账文件版本：后续新增字段只能追加，不能重排或删除既有键。
_LEDGER_SCHEMA_VERSION = 1


def _empty_ledger() -> dict[str, Any]:
    return {
        "schema_version": _LEDGER_SCHEMA_VERSION,
        "current_period": None,
        "ledger": [],          # API 配额消耗数据台账（追加型，每条记账一条）
        "alert_todos": [],     # 运行监控回写的告警待办
        "seq": {"ledger": 0, "alert": 0},
    }


class JsonLedgerStore:
    """文件型台账仓库：内存缓存 + 同步落盘。"""

    def __init__(self, path: str = _LEDGER_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            data = _empty_ledger()
            self._write_unlocked(data)
            return data
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            # 文件损坏时不静默吞掉业务数据：备份后重建空台账，保证服务可启动。
            backup = self.path + ".corrupt"
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            data = _empty_ledger()
            self._write_unlocked(data)
            return data
        # 仅追加式补齐新增的顶层键，老数据结构保持原样。
        base = _empty_ledger()
        for key, value in base.items():
            data.setdefault(key, value)
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".ledger-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ---- 读操作：直接返回内部结构的副本，避免调用方绕过锁修改 ----
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def current_period(self) -> str | None:
        with self._lock:
            return self._data.get("current_period")

    def set_current_period(self, period: str) -> None:
        with self._lock:
            self._data["current_period"] = period
            self._write_unlocked(self._data)

    def next_ledger_id(self) -> int:
        with self._lock:
            self._data["seq"]["ledger"] = int(self._data["seq"].get("ledger", 0)) + 1
            return int(self._data["seq"]["ledger"])

    def next_alert_id(self) -> int:
        with self._lock:
            self._data["seq"]["alert"] = int(self._data["seq"].get("alert", 0)) + 1
            return int(self._data["seq"]["alert"])

    def append_ledger_record(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._data["ledger"].append(record)
            self._write_unlocked(self._data)
            return json.loads(json.dumps(record, ensure_ascii=False))

    def update_ledger_record(self, record_id: int, changes: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            for record in self._data["ledger"]:
                if int(record.get("id", -1)) == record_id:
                    record.update(changes)
                    self._write_unlocked(self._data)
                    return json.loads(json.dumps(record, ensure_ascii=False))
            return None

    def find_ledger_by_request(self, request_no: str) -> dict[str, Any] | None:
        with self._lock:
            for record in self._data["ledger"]:
                if record.get("请求编号") == request_no:
                    return json.loads(json.dumps(record, ensure_ascii=False))
            return None

    def list_ledger(self) -> list[dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._data.get("ledger", []), ensure_ascii=False))

    def append_alert_todo(self, todo: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._data["alert_todos"].append(todo)
            self._write_unlocked(self._data)
            return json.loads(json.dumps(todo, ensure_ascii=False))

    def find_alert_by_key(self, idem_key: str) -> dict[str, Any] | None:
        with self._lock:
            for todo in self._data.get("alert_todos", []):
                if todo.get("幂等键") == idem_key:
                    return json.loads(json.dumps(todo, ensure_ascii=False))
            return None

    def list_alert_todos(self) -> list[dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._data.get("alert_todos", []), ensure_ascii=False))

    def flush(self) -> None:
        """显式同步落盘：采集API持久同步台账，刷新和返回后仍一致。"""
        with self._lock:
            self._write_unlocked(self._data)


# 单例：与 app.store.store 一样进程内共享，重启后从文件恢复。
ledger_store = JsonLedgerStore()
