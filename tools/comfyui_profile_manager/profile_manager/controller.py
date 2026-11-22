"""profile 切換狀態機。

所有外部副作用（systemctl、ComfyUI HTTP、/proc、nvidia-smi、時間）都由建構子注入，
測試時以假物件取代。subprocess 一律使用 list 參數，不使用 shell=True。

切換流程：取鎖 -> 檢查 profile -> 佇列必須為空 -> 記錄 last_good -> 停止前再查佇列
-> 寫 active -> restart -> 輪詢健康 -> 驗證版本與 MainPID cmdline
-> 成功更新 last_good；失敗則寫回舊 profile、restart、同樣驗證；回復也失敗 => RESCUE_NEEDED。
"""
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

try:  # shortcut: Windows 沒有 fcntl，退化為只用 threading.Lock；部署在 Linux 時兩者並用
    import fcntl
except ImportError:
    fcntl = None

from .config import NAME_RE

RESCUE_TEXT = ("自動回復失敗。請 SSH 到 Server：移除 /etc/systemd/system/comfyui.service.d/20-profile.conf，"
               "systemctl daemon-reload，systemctl restart comfyui.service（詳見 README，勿用 systemctl revert）。")


class SwitchRefused(Exception):
    """切換請求在動手前就被拒絕（不會改動任何狀態）。"""


class ServiceError(Exception):
    pass


class StateStore:
    """state 目錄：只存 active / last_good 兩個名稱檔。讀取時再驗證白名單。"""

    def __init__(self, directory):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def get(self, key):
        try:
            with open(os.path.join(self.dir, key), encoding="utf-8") as f:
                v = f.read().strip()
        except OSError:
            return None
        return v if NAME_RE.match(v) else None

    def set(self, key, value):
        if not NAME_RE.match(value):
            raise ValueError("名稱不合法")
        tmp = os.path.join(self.dir, key + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(value + "\n")
        os.replace(tmp, os.path.join(self.dir, key))  # 原子替換


class SystemctlService:
    """透過 polkit 授權的 systemctl（僅 comfyui.service 的 restart）。"""
    UNIT = "comfyui.service"

    def restart(self):
        r = subprocess.run(["systemctl", "--no-ask-password", "restart", self.UNIT],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise ServiceError(f"systemctl restart 失敗：{r.stderr.strip()}")

    def main_pid(self):
        r = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", self.UNIT],
                           capture_output=True, text=True, timeout=10)
        return int(r.stdout.strip() or 0)


class HttpComfy:
    """ComfyUI 的 /queue 與 /system_stats。"""

    def __init__(self, base_url="http://127.0.0.1:8188", timeout=3):
        self.base, self.timeout = base_url.rstrip("/"), timeout

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            e.close()  # 避免洩漏 socket
            raise

    def queue_counts(self):
        q = self._get("/queue")
        return len(q.get("queue_running", [])), len(q.get("queue_pending", []))

    def stats(self):
        return self._get("/system_stats")


def read_proc_cmdline(pid):
    with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
        return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]


def nvidia_vram():
    """回傳 {used_mb,total_mb}；取不到（無 nvidia-smi 等）回傳 None。固定參數、不經 shell。"""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        used, total = [int(x) for x in r.stdout.splitlines()[0].split(",")]
        return {"used_mb": used, "total_mb": total}
    except Exception:
        return None


def _versions(stats):
    s = stats.get("system", stats)
    return str(s.get("python_version", "")), str(s.get("pytorch_version", ""))


def _installed(p):
    return os.path.exists(p.python) and os.path.exists(p.app_dir.rstrip("/\\") + "/main.py")


class Controller:
    def __init__(self, profiles, store, service, comfy, proc_cmdline, vram,
                 sleep=time.sleep, monotonic=time.monotonic, health_timeout=180, poll_interval=2):
        self.profiles, self.store, self.service, self.comfy = profiles, store, service, comfy
        self.proc_cmdline, self.vram, self.sleep, self.monotonic = proc_cmdline, vram, sleep, monotonic
        self.health_timeout, self.poll_interval = health_timeout, poll_interval
        self._lock = threading.Lock()
        self._lockfile = None
        self._slock = threading.Lock()  # 保護 _state
        self._state = {"state": "IDLE", "steps": [], "result": None}

    # ---- 狀態 ----
    def _step(self, msg):
        with self._slock:
            self._state["steps"].append(msg)

    def snapshot(self):
        with self._slock:
            return {**self._state, "steps": list(self._state["steps"])}

    def _finish(self, outcome, reason, state="IDLE"):
        with self._slock:
            self._state["state"] = state
            self._state["result"] = {"outcome": outcome, "reason": reason}
            self._state["steps"].append(f"{outcome}: {reason}")

    def live(self):
        """即時資訊（健康、版本、佇列、VRAM）；任何一項失敗只影響該項。"""
        out = {"healthy": False, "python": None, "pytorch": None, "device": None,
               "running": None, "pending": None, "vram": None}
        try:
            st = self.comfy.stats()
            out["healthy"] = True
            out["python"], out["pytorch"] = _versions(st)
            devs = st.get("devices") or [{}]
            out["device"] = devs[0].get("name")
            out["running"], out["pending"] = self.comfy.queue_counts()
        except Exception:
            pass
        out["vram"] = self.vram()
        return out

    def profile_list(self):
        active = self.store.get("active")
        return [{"name": p.name, "display_name": p.display_name, "validated": p.validated,
                 "installed": _installed(p), "active": p.name == active,
                 "expected_python": p.expected_python, "expected_torch": p.expected_torch}
                for p in self.profiles.values()]

    def status(self):
        snap = self.snapshot()
        return {**snap, "active": self.store.get("active"), "last_good": self.store.get("last_good"),
                "profiles": self.profile_list(), "live": self.live(),
                "rescue_text": RESCUE_TEXT if snap["state"] == "RESCUE_NEEDED" else None}

    # ---- 鎖 ----
    def _acquire(self):
        if not self._lock.acquire(blocking=False):
            return False
        if fcntl:
            try:
                self._lockfile = open(os.path.join(self.store.dir, "switch.lock"), "w")
                fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._lock.release()
                return False
        return True

    def _release(self):
        if self._lockfile:
            self._lockfile.close()  # 關檔即釋放 flock
            self._lockfile = None
        self._lock.release()

    # ---- 切換 ----
    def _precheck(self, name):
        """動手前的所有檢查；不通過丟 SwitchRefused。"""
        if self.snapshot()["state"] == "RESCUE_NEEDED":
            raise SwitchRefused("系統處於 RESCUE_NEEDED，需先手動救援並重啟管理服務")
        if not isinstance(name, str) or not NAME_RE.match(name) or name not in self.profiles:
            raise SwitchRefused("無效的 profile 名稱")
        p = self.profiles[name]
        if not p.validated:
            raise SwitchRefused(f"profile {name} 尚未通過人工驗證 (validated=false)")
        if not _installed(p):
            raise SwitchRefused(f"profile {name} 的 python 或 main.py 不存在")
        if self.store.get("active") is None:
            raise SwitchRefused("找不到目前 active profile")
        if name == self.store.get("active"):
            raise SwitchRefused("已是目前使用中的 profile")
        try:
            running, pending = self.comfy.queue_counts()
        except Exception as e:
            raise SwitchRefused(f"無法確認 ComfyUI 佇列狀態：{e}")
        if running or pending:
            raise SwitchRefused(f"ComfyUI 佇列忙碌（執行中 {running}、等待 {pending}），請稍後再試")
        return p

    def start_switch(self, name, background=True):
        """通過檢查後開始切換；background=True 時於背景 thread 執行。被拒絕丟 SwitchRefused。"""
        if not self._acquire():
            raise SwitchRefused("已有切換正在進行")
        try:
            target = self._precheck(name)
        except Exception:
            self._release()
            raise
        with self._slock:
            self._state = {"state": "SWITCHING", "steps": [f"開始切換到 {name}"], "result": None}
        if background:
            threading.Thread(target=self._run, args=(target,), daemon=True).start()
        else:
            self._run(target)

    def _run(self, target):
        try:
            prev_name = self.store.get("active")
            self.store.set("last_good", prev_name)
            self._step("停止前再次檢查佇列")
            try:
                busy = any(self.comfy.queue_counts())
            except Exception:
                busy = True
            if busy:
                return self._finish("ABORTED", "停止前發現佇列忙碌或無法確認，已中止，未做任何變更")
            try:
                self._step(f"寫入 active={target.name} 並重啟 comfyui.service")
                self.store.set("active", target.name)
                self.service.restart()
                self._wait_ready(target)
            except Exception as e:
                return self._rollback(prev_name, str(e))
            self.store.set("last_good", target.name)
            self._finish("SUCCESS", f"已切換到 {target.name}，健康與版本驗證通過")
        except Exception as e:  # 防止 thread 靜默死亡而狀態卡在 SWITCHING
            self._finish("RESCUE_NEEDED", f"內部錯誤：{e}。{RESCUE_TEXT}", state="RESCUE_NEEDED")
        finally:
            self._release()

    def _rollback(self, prev_name, why):
        self._step(f"切換失敗（{why}），回復到 {prev_name}")
        try:
            prev = self.profiles[prev_name]
            self.store.set("active", prev_name)
            self.service.restart()
            self._wait_ready(prev)
        except Exception as e:
            return self._finish("RESCUE_NEEDED", f"切換失敗（{why}）；自動回復失敗（{e}）。{RESCUE_TEXT}",
                                state="RESCUE_NEEDED")
        self._finish("ROLLED_BACK", f"切換失敗（{why}）；已自動回復到 {prev_name} 並驗證健康")

    def _wait_ready(self, p):
        """輪詢到健康；驗證版本前綴與 MainPID cmdline argv[0]；任何不符丟 RuntimeError。"""
        self._step(f"等待 {p.name} 健康檢查")
        deadline = self.monotonic() + self.health_timeout
        while True:
            try:
                stats = self.comfy.stats()
                break
            except Exception:
                if self.monotonic() >= deadline:
                    raise RuntimeError(f"健康檢查逾時（{self.health_timeout}s）")
                self.sleep(self.poll_interval)
        py, torch = _versions(stats)
        if not py.startswith(p.expected_python) or not torch.startswith(p.expected_torch):
            raise RuntimeError(f"版本不符：python={py} torch={torch}，期望 {p.expected_python}/{p.expected_torch}")
        pid = self.service.main_pid()
        try:
            argv = self.proc_cmdline(pid)
        except Exception as e:
            raise RuntimeError(f"無法讀取 /proc/{pid}/cmdline：{e}")
        if not argv or argv[0] != p.python:
            raise RuntimeError(f"MainPID 的執行檔 {argv[:1]} 不等於 profile python {p.python}")
