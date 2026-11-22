"""controller 的 HTTP 介面（ThreadingHTTPServer，預設只聽 127.0.0.1:8189）。

端點：GET / 、GET /api/status、GET /api/profiles、POST /api/login、/api/logout、/api/switch。
沒有任何執行任意命令的端點；switch 只接受 profile 名稱，並由 controller 比對白名單。
所有請求檢查 Host（防 DNS rebinding）；POST 另檢查 Origin；變更類 POST 需 session＋CSRF header。
"""
import argparse
import hmac
import json
import os
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .auth import RateLimiter, SessionStore, verify_password
from .config import load_profiles
from .controller import (Controller, HttpComfy, StateStore, SwitchRefused, SystemctlService,
                         nvidia_vram, read_proc_cmdline)

COOKIE = "pm_session"
INDEX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "index.html")


class App:
    def __init__(self, controller, password_hash, sessions=None, limiter=None):
        self.ctl, self.pw_hash = controller, password_hash
        self.sessions, self.limiter = sessions or SessionStore(), limiter or RateLimiter()
        self.allowed_hosts = set()  # 綁定埠後由 make_server 填入


class Handler(BaseHTTPRequestHandler):
    app = None  # 由 make_server 以子類別注入
    server_version = "ProfileManager"

    def log_message(self, fmt, *args):  # 不把請求內容（可能含密碼）寫進日誌
        pass

    # ---- 工具 ----
    def _send(self, code, obj=None, headers=(), body=None, ctype="application/json"):
        data = body if body is not None else json.dumps(obj or {}, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        return self.headers.get("Host", "") in self.app.allowed_hosts

    def _origin_ok(self):
        o = urlparse(self.headers.get("Origin", ""))
        return o.scheme == "http" and o.netloc == self.headers.get("Host")

    def _token(self):
        try:
            c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return c[COOKIE].value if COOKIE in c else None
        except cookies.CookieError:
            return None

    def _json_body(self):
        raw = self.headers.get("Content-Length") or "0"
        if not raw.isdigit() or int(raw) > 4096:  # 負數、非數字、過大一律拒絕
            raise ValueError("Content-Length 不合法")
        body = json.loads(self.rfile.read(int(raw)) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("JSON body 必須是物件")
        return body

    # ---- 路由 ----
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "Host 不允許"})
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(INDEX, "rb") as f:
                return self._send(200, body=f.read(), ctype="text/html")
        if path in ("/api/status", "/api/profiles"):
            csrf = self.app.sessions.csrf_for(self._token())
            if csrf is None:
                return self._send(401, {"error": "請先登入"})
            st = self.app.ctl.status()
            st["csrf"] = csrf
            return self._send(200, st if path == "/api/status" else {"profiles": st["profiles"]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._send(403, {"error": "Host/Origin 不允許"})
        path = urlparse(self.path).path
        try:
            body = self._json_body()
        except Exception:
            return self._send(400, {"error": "JSON 格式錯誤"})
        if path == "/api/login":
            return self._login(body)
        token = self._token()
        csrf = self.app.sessions.csrf_for(token)
        if csrf is None:
            return self._send(401, {"error": "請先登入"})
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf):
            return self._send(403, {"error": "CSRF token 錯誤"})
        if path == "/api/logout":
            self.app.sessions.delete(token)
            return self._send(200, {"ok": True}, [("Set-Cookie", f"{COOKIE}=; Max-Age=0; Path=/")])
        if path == "/api/switch":
            try:
                self.app.ctl.start_switch(body.get("profile"))
            except SwitchRefused as e:
                return self._send(409, {"error": str(e)})
            return self._send(202, {"accepted": True})
        self._send(404, {"error": "not found"})

    def _login(self, body):
        ip = self.client_address[0]
        if self.app.limiter.blocked(ip):
            return self._send(429, {"error": "登入失敗次數過多，請稍後再試"})
        pw = body.get("password")
        if not (isinstance(pw, str) and verify_password(pw, self.app.pw_hash)):
            self.app.limiter.fail(ip)
            return self._send(401, {"error": "密碼錯誤"})
        self.app.limiter.clear(ip)
        token, csrf = self.app.sessions.create()
        cookie = f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"
        self._send(200, {"csrf": csrf}, [("Set-Cookie", cookie)])


def make_server(app, host="127.0.0.1", port=8189):
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((host, port), handler)
    p = srv.server_address[1]
    app.allowed_hosts = {f"127.0.0.1:{p}", f"localhost:{p}", f"[::1]:{p}"}
    return srv


def main():
    ap = argparse.ArgumentParser(description="ComfyUI Profile Manager")
    ap.add_argument("--profiles", default="/etc/comfyui-profiles/profiles.toml")
    ap.add_argument("--state-dir", default="/var/lib/comfyui-profile-manager")
    ap.add_argument("--auth-file", default="/etc/comfyui-profiles/admin.hash")
    ap.add_argument("--comfy-url", default="http://127.0.0.1:8188")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8189)
    ap.add_argument("--health-timeout", type=int, default=180)
    a = ap.parse_args()
    with open(a.auth_file, encoding="utf-8") as f:
        pw_hash = f.read().strip()
    ctl = Controller(load_profiles(a.profiles), StateStore(a.state_dir), SystemctlService(),
                     HttpComfy(a.comfy_url), read_proc_cmdline, nvidia_vram, health_timeout=a.health_timeout)
    make_server(App(ctl, pw_hash), a.host, a.port).serve_forever()


if __name__ == "__main__":
    main()
