"""測試用假環境：以一個 World 同時模擬 systemctl、ComfyUI HTTP、/proc。

行為：service.restart() 會讀 active 檔，依 profile 名稱決定「啟動失敗」「永遠不健康」
「版本不符」「cmdline 不符」，其餘視為正常啟動。完全離線。
"""
import os
import tempfile

from profile_manager.config import Profile
from profile_manager.controller import Controller, StateStore, ServiceError


class FakeClock:
    """假時鐘：sleep 只推進時間，測試不需真的等待。"""

    def __init__(self):
        self.now = 0.0

    def sleep(self, s):
        self.now += s

    def monotonic(self):
        return self.now


class World:
    def __init__(self, store):
        self.store = store
        self.running = "standard"  # 目前實際在跑的 profile
        self.up = True
        self.queue = ([], [])  # (running, pending)
        self.queue_hook = None  # 每次 queue() 呼叫後執行，可用來模擬佇列變忙
        self.fail_start, self.never_healthy = set(), set()
        self.bad_torch, self.bad_cmdline = set(), set()
        self.restarts = []
        self.profiles = {}

    # --- service 介面 ---
    def restart(self):
        name = self.store.get("active")
        self.restarts.append(name)
        if name in self.fail_start:
            self.up = False
            raise ServiceError("systemctl restart 失敗")
        self.running = name
        self.up = name not in self.never_healthy

    def main_pid(self):
        return 4242

    # --- comfy 介面 ---
    def queue_counts(self):
        r, p = self.queue
        out = (len(r), len(p))
        if self.queue_hook:
            self.queue_hook()
        return out

    def stats(self):
        if not self.up:
            raise ConnectionError("down")
        pf = self.profiles[self.running]
        torch = "0.0.0" if self.running in self.bad_torch else pf.expected_torch + "+cu130"
        return {"system": {"python_version": pf.expected_python + ".1", "pytorch_version": torch},
                "devices": [{"name": "cuda:0 FakeGPU"}]}

    # --- /proc ---
    def cmdline(self, pid):
        pf = self.profiles[self.running]
        return ["/wrong/python"] if self.running in self.bad_cmdline else [pf.python, "main.py"]


def make_profiles(tmp):
    """建立 standard(已驗證)、alt(已驗證)、raw(未驗證)、ghost(python 不存在) 四個 profile。"""
    app = os.path.join(tmp, "app")
    os.makedirs(app, exist_ok=True)
    open(os.path.join(app, "main.py"), "w").close()
    out = {}
    for name, validated in (("standard", True), ("alt", True), ("raw", False), ("ghost", True)):
        py = os.path.join(tmp, "py_" + name)
        if name != "ghost":
            open(py, "w").close()
        out[name] = Profile(name, name.title(), py, app, ("--port", "8188"),
                            "3.12", "2.14.1", validated)
    return out


def make_controller(**kw):
    tmp = tempfile.mkdtemp()
    profiles = make_profiles(tmp)
    store = StateStore(os.path.join(tmp, "state"))
    store.set("active", "standard")
    world = World(store)
    world.profiles = profiles
    clock = FakeClock()
    ctl = Controller(profiles, store, world, world, world.cmdline, lambda: None,
                     sleep=clock.sleep, monotonic=clock.monotonic,
                     health_timeout=30, poll_interval=1, **kw)
    return ctl, world, store
