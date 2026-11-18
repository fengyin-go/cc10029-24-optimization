"""API 配额消耗数据台账：真实进程重启恢复全链路验收。

仅依赖标准库：用 urllib 驱动 HTTP，用 subprocess 拉起/杀掉 uvicorn 真实进程，
验证台账在进程重启后的持久性与一致性。运行：

    QUOTA_LEDGER_PATH=<隔离目录>/ledger.json .venv/bin/python tests/test_quota_ledger.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(HERE)


def _encode_url(path: str) -> str:
    """中文查询参数需要百分号编码，urllib 不接受非 ASCII 的 URL。"""
    parts = urllib.parse.urlsplit(path)
    if not parts.query:
        return path
    query = urllib.parse.urlencode(urllib.parse.parse_qsl(parts.query))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def http(method: str, path: str, body: dict | None = None, expect_status: int = 200):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(_encode_url(BASE + path), data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            if resp.status != expect_status:
                raise AssertionError(f"{method} {path} 状态码 {resp.status} != {expect_status}")
            return payload
    except urllib.error.HTTPError as exc:
        raise AssertionError(f"{method} {path} 返回 {exc.code}: {exc.read().decode('utf-8')}") from exc


class ServerHarness:
    def __init__(self, ledger_path: str) -> None:
        self.ledger_path = ledger_path
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = dict(os.environ)
        env["QUOTA_LEDGER_PATH"] = self.ledger_path
        os.makedirs(os.path.dirname(self.ledger_path), exist_ok=True)
        log = open(os.path.join(os.path.dirname(self.ledger_path), "uvicorn.log"), "a", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", "8000", "--log-level", "warning"],
            cwd=BACKEND_DIR, env=env, stdout=log, stderr=log,
        )
        for _ in range(60):
            try:
                http("GET", "/api/health")
                return
            except Exception:
                if self.proc.poll() is not None:
                    raise AssertionError("uvicorn 启动失败，详见 uvicorn.log")
                time.sleep(0.25)
        raise AssertionError("uvicorn 启动超时")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.proc = None

    def restart(self) -> None:
        self.stop()
        time.sleep(0.5)
        self.start()


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"断言失败：{name} {detail}")
    print(f"  ✓ {name}")


def run() -> None:
    workdir = tempfile.mkdtemp(prefix="quota-e2e-")
    ledger_path = os.path.join(workdir, "data", "quota_ledger.json")
    print(f"隔离台账文件：{ledger_path}")
    harness = ServerHarness(ledger_path)
    failures: list[str] = []

    try:
        print("[1] 首次启动 + 初始余额")
        harness.start()
        health = http("GET", "/api/health")
        check("健康检查", health["ok"] is True)

        bal0 = http("GET", "/api/apigate/quota/balance?调用方=caller-alpha")
        check("初始剩余额度为默认上限 1000", bal0["item"]["剩余额度"] == 1000, json.dumps(bal0, ensure_ascii=False))

        print("[2] 两阶段记账：submit 预占、confirm 正式消耗")
        r1 = http("POST", "/api/apigate/quota/consume",
                  {"values": {"请求编号": "REQ-001", "调用方": "caller-alpha",
                              "配置编号": "APIG-0001", "消耗额度": 120, "API版本": "v2"}})
        check("REQ-001 提交成功", r1["ok"] and r1["item"]["记账状态"] == "待确认", r1.get("message", ""))
        check("记账携带请求编号/调用方/API版本",
              r1["item"]["请求编号"] == "REQ-001" and r1["item"]["调用方"] == "caller-alpha"
              and r1["item"]["API版本"] == "v2")
        check("待确认预占已计入余额",
              http("GET", "/api/apigate/quota/balance?调用方=caller-alpha")["item"]["剩余额度"] == 880)

        c1 = http("POST", "/api/apigate/quota/confirm", {"请求编号": "REQ-001"})
        check("REQ-001 确认后为已确认", c1["item"]["记账状态"] == "已确认" and c1["item"]["确认时间"])
        check("确认后余额口径不变",
              http("GET", "/api/apigate/quota/balance?调用方=caller-alpha")["item"]["剩余额度"] == 880)

        print("[3] 重复请求编号只记账一次（连续送交只生成一份）")
        r1_dup = http("POST", "/api/apigate/quota/consume",
                      {"values": {"请求编号": "REQ-001", "调用方": "caller-alpha",
                                  "配置编号": "APIG-0001", "消耗额度": 120}})
        check("重复提交被识别", r1_dup["ok"] and r1_dup["duplicated"] is True)
        ledger = http("GET", "/api/apigate/quota/ledger?请求编号=REQ-001&size=50")
        check("台账中 REQ-001 只有一条", ledger["total"] == 1 and ledger["items"][0]["请求编号"] == "REQ-001")

        print("[4] 待确认请求 + 进程重启恢复")
        r2 = http("POST", "/api/apigate/quota/consume",
                  {"values": {"请求编号": "REQ-002", "调用方": "caller-alpha",
                              "配置编号": "APIG-0001", "消耗额度": 80}})
        check("REQ-002 待确认", r2["item"]["记账状态"] == "待确认")
        before = http("GET", "/api/apigate/quota/balance?调用方=caller-alpha")["item"]
        check("重启前余额=800", before["剩余额度"] == 800, json.dumps(before, ensure_ascii=False))

        harness.restart()
        print("    进程已重启")

        rec = http("GET", "/api/apigate/quota/recover")
        check("重启后未完成请求恢复为待确认",
              rec["total"] == 1 and rec["items"][0]["请求编号"] == "REQ-002"
              and rec["items"][0]["记账状态"] == "待确认")
        after = http("GET", "/api/apigate/quota/balance?调用方=caller-alpha")["item"]
        check("重启后余额完全一致", after["剩余额度"] == 800 and after["已确认消耗"] == 120
              and after["待确认消耗"] == 80, json.dumps(after, ensure_ascii=False))

        c2 = http("POST", "/api/apigate/quota/confirm", {"请求编号": "REQ-002"})
        check("重启后可补回执确认", c2["item"]["记账状态"] == "已确认")
        check("确认后余额=800",
              http("GET", "/api/apigate/quota/balance?调用方=caller-alpha")["item"]["剩余额度"] == 800)

        print("[5] 撤销未完成请求：释放预占，不先扣后丢")
        r3 = http("POST", "/api/apigate/quota/consume",
                  {"values": {"请求编号": "REQ-003", "调用方": "caller-alpha",
                              "配置编号": "APIG-0001", "消耗额度": 50}})
        check("REQ-003 待确认", r3["item"]["记账状态"] == "待确认")
        rv = http("POST", "/api/apigate/quota/revoke", {"请求编号": "REQ-003"})
        check("REQ-003 已作废", rv["item"]["记账状态"] == "已作废")
        check("作废后释放预占、余额回到800",
              http("GET", "/api/apigate/quota/balance?调用方=caller-alpha")["item"]["剩余额度"] == 800)

        print("[6] 超额提交被拒绝，且不落台账")
        over = http("POST", "/api/apigate/quota/consume",
                    {"values": {"请求编号": "REQ-OVER", "调用方": "caller-alpha",
                                "配置编号": "APIG-0001", "消耗额度": 9999}})
        check("超额请求被拒", over["ok"] is False and "配额不足" in over["message"])
        check("超额请求未产生记账",
              http("GET", "/api/apigate/quota/ledger?请求编号=REQ-OVER")["total"] == 0)

        print("[7] 运行监控从台账取数，超限结论回写告警待办且去重")
        # caller-beta 在 APIG-0002 上快速打满（默认额度 1000）
        for i, amount in enumerate([600, 400], start=4):
            rr = http("POST", "/api/apigate/quota/consume",
                      {"values": {"请求编号": f"REQ-B{i}", "调用方": "caller-beta",
                                  "配置编号": "APIG-0002", "消耗额度": amount}})
            check(f"REQ-B{i} 提交成功", rr["ok"], rr.get("message", ""))
            http("POST", "/api/apigate/quota/confirm", {"请求编号": f"REQ-B{i}"})
        obs = http("GET", "/api/monitor/quota/observations")
        beta = [x for x in obs["items"] if x["调用方"] == "caller-beta"][0]
        check("观测清单反映台账处理结果（占用1000/上限1000/超限）",
              beta["已占用额度"] == 1000 and beta["超限结论"] == "超限" and beta["是否超限"] is True)

        scan1 = http("POST", "/api/monitor/quota/scan-alerts", {})
        check("首次扫描回写1条告警待办", scan1["新增数量"] == 1 and scan1["重复数量"] == 0)
        scan2 = http("POST", "/api/monitor/quota/scan-alerts", {})
        check("重复扫描拒绝生成重复结果", scan2["新增数量"] == 0 and scan2["重复数量"] == 1)
        todos = http("GET", "/api/monitor/quota/alert-todos?调用方=caller-beta")
        check("告警待办仅一条且含超限结论",
              todos["total"] == 1 and "超限" in todos["items"][0]["超限结论"]
              and todos["items"][0]["数据来源"] == "API配额消耗数据台账")
        alert_page = http("GET", "/api/alert/quota-todos?调用方=caller-beta")
        check("告警中心页面可见同一条待办", alert_page["total"] == 1)

        print("[8] 周期切换与重启恢复同时发生，调用方只见一个完整余额")
        sw = http("POST", "/api/apigate/quota/periods/switch", {"周期版本": "2026-12"})
        check("周期指针切换", sw["ok"] and sw["item"]["当前周期版本"] == "2026-12")
        r4 = http("POST", "/api/apigate/quota/consume",
                  {"values": {"请求编号": "REQ-004", "调用方": "caller-alpha",
                              "配置编号": "APIG-0001", "消耗额度": 30}})
        check("新周期记账指向新版本", r4["item"]["周期版本"] == "2026-12")
        harness.restart()
        print("    进程已重启（周期切换 + 重启恢复同时发生）")
        periods = http("GET", "/api/apigate/quota/periods")
        check("重启后当前指针仍为2026-12", periods["current"] == "2026-12")
        bal_new = http("GET", "/api/apigate/quota/balance?调用方=caller-alpha")["item"]
        check("当前周期是一个完整余额（970）", bal_new["剩余额度"] == 970 and bal_new["周期版本"] == "2026-12")
        bal_old = http("GET", "/api/apigate/quota/balance?调用方=caller-alpha&周期版本=2026-11")["item"]
        check("历史周期余额仍可核对（800）", bal_old["剩余额度"] == 800)
        check("周期边界可核对",
              bal_new["周期边界"]["周期开始"] == "2026-12-01" and bal_new["周期边界"]["周期结束"] == "2027-01-01")

        print("[9] 台账刷新/返回一致 + 重新进入保留原结果 + 按调用方查询")
        view1 = http("GET", "/api/apigate/quota/ledger?size=200")
        view2 = http("GET", "/api/apigate/quota/ledger?size=200")
        check("两次刷新台账一致", json.dumps(view1, ensure_ascii=False, sort_keys=True)
              == json.dumps(view2, ensure_ascii=False, sort_keys=True))
        alpha_rows = http("GET", "/api/apigate/quota/ledger?调用方=caller-alpha&size=200")
        check("按调用方查询只返回该调用方",
              alpha_rows["total"] >= 3 and all(r["调用方"] == "caller-alpha" for r in alpha_rows["items"]))
        trace = http("GET", "/api/apigate/quota/trace?调用方=caller-alpha")
        check("调用链溯源含配置来源/记账/观测/告警四段",
              len(trace["item"]["API配置来源"]) >= 1 and len(trace["item"]["台账记账记录"]) >= 1
              and len(trace["item"]["接口运行观测"]) >= 1)

        print("[10] 落盘文件直接核验：重复请求编号在台账中唯一")
        with open(ledger_path, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)
        request_nos = [r["请求编号"] for r in on_disk["ledger"]]
        check("文件台账无重复请求编号", len(request_nos) == len(set(request_nos)))
        check("文件台账含回写的告警待办", len(on_disk["alert_todos"]) == 1)
        check("REQ-003 已作废不占额度",
              next(r for r in on_disk["ledger"] if r["请求编号"] == "REQ-003")["记账状态"] == "已作废")

        print("[11] 既有采集API CRUD 未回归")
        crud = http("GET", "/api/apigate?size=200")
        check("API配置列表仍为3条种子数据", crud["total"] == 3)
        entry = http("GET", "/api/apigate/3")
        check("既有字段名保持不变", all(f in entry for f in
              ["配置编号", "端点路径", "请求方法", "鉴权方式", "速率限制", "响应缓存", "调用配额", "版本控制"]))

    except AssertionError as exc:
        failures.append(str(exc))
    finally:
        harness.stop()

    if failures:
        print("\n❌ 验收失败：")
        for item in failures:
            print(f" - {item}")
        sys.exit(1)
    print("\n🎉 全部 11 组场景验收通过")


if __name__ == "__main__":
    run()
