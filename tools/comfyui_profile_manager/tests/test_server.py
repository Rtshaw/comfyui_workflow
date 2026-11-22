"""HTTP 層測試（驗證 / CSRF / Origin）與模擬整合測試（真 HTTP server + 假 ComfyUI + 假 service）。"""
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from profile_manager.auth import hash_password
from profile_manager.controller import Controller, HttpComfy, ServiceError, StateStore
from profile_manager.server import App, make_server
from tests.fakes import make_profiles

PW = "s3cret-pass"


class FakeComfyServer:
    """假 ComfyUI：/queue 與 /system_stats；版本由目前「執行中的 profile」決定。"""

    def __init__(self, profiles):
        self.profiles, self.running, self.up = profiles, "standard", True
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if not outer.up:
                    self.send_error(503)
                    return
                pf = outer.profiles[outer.running]
                if self.path == "/queue":
                    body = {"queue_running": [], "queue_pending": []}
                else:
                    body = {"system": {"python_version": pf.expected_python + ".1",
                                       "pytorch_version": pf.expected_torch + "+cu130"}, "devices": [{"name": "FakeGPU"}]}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


class FakeSystemctl:
    """假 service：restart 依 active 檔更新假 ComfyUI；fail 集合內的 profile 啟動後永不健康。"""

    def __init__(self, store, comfy, profiles):
        self.store, self.comfy, self.profiles, self.fail = store, comfy, profiles, set()

    def restart(self):
        name = self.store.get("active")
        self.comfy.running = name
        self.comfy.up = name not in self.fail

    def main_pid(self):
        return 1


class ServerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.profiles = make_profiles(tmp)
        store = StateStore(os.path.join(tmp, "state"))
        store.set("active", "standard")
        self.comfy = FakeComfyServer(self.profiles)
        self.svc = FakeSystemctl(store, self.comfy, self.profiles)
        ctl = Controller(self.profiles, store, self.svc, HttpComfy(self.comfy.url, timeout=1),
                         lambda pid: [self.profiles[self.comfy.running].python], lambda: None,
                         health_timeout=1, poll_interval=0.05)
        self.srv = make_server(App(ctl, hash_password(PW, n=2 ** 10)), "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        for s_ in (self.srv, self.comfy.srv):  # 關閉監聽 socket，避免 ResourceWarning
            self.addCleanup(s_.server_close)
            self.addCleanup(s_.shutdown)

    def req(self, method, path, body=None, headers=None, origin=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        if origin and method == "POST":
            h.setdefault("Origin", f"http://127.0.0.1:{self.port}")
        c.request(method, path, json.dumps(body or {}) if method == "POST" else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, (json.loads(data) if data[:1] == b"{" else data)

    def login(self):
        r, b = self.req("POST", "/api/login", {"password": PW})
        cookie = r.getheader("Set-Cookie")
        return {"Cookie": cookie.split(";")[0], "X-CSRF-Token": b["csrf"]}, cookie

    def wait_idle(self, h):
        for _ in range(100):
            _, st = self.req("GET", "/api/status", headers=h)
            if st["state"] != "SWITCHING":
                return st
            time.sleep(0.05)
        self.fail("切換未結束")


class AuthTests(ServerCase):
    def test_unauthenticated_rejected(self):
        self.assertEqual(self.req("GET", "/api/status")[0].status, 401)
        self.assertEqual(self.req("POST", "/api/switch", {"profile": "alt"})[0].status, 401)

    def test_wrong_password_and_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.req("POST", "/api/login", {"password": "x"})[0].status, 401)
        self.assertEqual(self.req("POST", "/api/login", {"password": PW})[0].status, 429)

    def test_cookie_flags(self):
        _, cookie = self.login()
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_missing_csrf_rejected(self):
        h, _ = self.login()
        h.pop("X-CSRF-Token")
        self.assertEqual(self.req("POST", "/api/switch", {"profile": "alt"}, h)[0].status, 403)
        self.assertEqual(self.comfy.running, "standard")

    def test_bad_origin_rejected(self):
        h, _ = self.login()
        h["Origin"] = "http://evil.example"
        self.assertEqual(self.req("POST", "/api/switch", {"profile": "alt"}, h)[0].status, 403)
        self.assertEqual(self.req("POST", "/api/switch", {"profile": "alt"}, {k: v for k, v in h.items() if k != "Origin"},
                                  origin=False)[0].status, 403)

    def test_bad_host_rejected(self):
        self.assertEqual(self.req("GET", "/", headers={"Host": "evil.example"})[0].status, 403)

    def test_bad_content_length_and_non_dict_body(self):
        for cl, payload in (("-1", b""), ("abc", b""), ("2", b"[]"), ("4", b"null")):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.putrequest("POST", "/api/login")
            c.putheader("Origin", f"http://127.0.0.1:{self.port}")
            c.putheader("Content-Length", cl)
            c.endheaders(payload)
            r = c.getresponse()
            r.read()
            c.close()
            self.assertEqual(r.status, 400, cl)

    def test_invalid_profile_name_rejected(self):
        h, _ = self.login()
        self.assertEqual(self.req("POST", "/api/switch", {"profile": "../x; rm"}, h)[0].status, 409)


class IntegrationTests(ServerCase):
    def test_login_switch_success(self):
        h, _ = self.login()
        r, _ = self.req("POST", "/api/switch", {"profile": "alt"}, h)
        self.assertEqual(r.status, 202)
        st = self.wait_idle(h)
        self.assertEqual(st["result"]["outcome"], "SUCCESS")
        self.assertEqual((st["active"], self.comfy.running), ("alt", "alt"))

    def test_switch_fail_then_rollback(self):
        self.svc.fail.add("alt")
        h, _ = self.login()
        self.req("POST", "/api/switch", {"profile": "alt"}, h)
        st = self.wait_idle(h)
        self.assertEqual(st["result"]["outcome"], "ROLLED_BACK")
        self.assertEqual((st["active"], self.comfy.running), ("standard", "standard"))
        self.assertTrue(st["live"]["healthy"])


if __name__ == "__main__":
    unittest.main()
