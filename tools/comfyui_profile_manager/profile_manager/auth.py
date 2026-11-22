"""驗證：scrypt 密碼雜湊、session/CSRF token、登入失敗限速。只用標準函式庫。

雜湊格式：scrypt$N$r$p$salt_b64$hash_b64（單行文字，存於 root 擁有的設定檔）。
"""
import base64
import hashlib
import hmac
import os
import secrets
import threading
import time


def hash_password(password, n=2 ** 14, r=8, p=1):
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    b = lambda x: base64.b64encode(x).decode()
    return f"scrypt${n}${r}${p}${b(salt)}${b(h)}"


def verify_password(password, stored):
    """格式錯誤或不符一律回 False（不丟例外，避免洩漏細節）。"""
    try:
        _, n, r, p, salt, expect = stored.strip().split("$")
        h = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                           n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(h, base64.b64decode(expect))
    except Exception:
        return False


class SessionStore:
    """記憶體 session：token -> (csrf, 到期時間)。重啟服務即全部登出。"""

    def __init__(self, ttl=8 * 3600, monotonic=time.monotonic):
        self.ttl, self.mono = ttl, monotonic
        self._d, self._lock = {}, threading.Lock()

    def create(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self._lock:
            self._d[token] = (csrf, self.mono() + self.ttl)
        return token, csrf

    def csrf_for(self, token):
        """token 有效回傳其 csrf，否則 None（並清除過期項）。"""
        with self._lock:
            item = self._d.get(token)
            if item and item[1] < self.mono():
                del self._d[token]
                item = None
            return item[0] if item else None

    def delete(self, token):
        with self._lock:
            self._d.pop(token, None)


class RateLimiter:
    """同一來源在 window 秒內失敗 max_fail 次即鎖定。"""

    def __init__(self, max_fail=5, window=300, monotonic=time.monotonic):
        self.max, self.window, self.mono = max_fail, window, monotonic
        self._fails, self._lock = {}, threading.Lock()

    def _recent(self, key):
        now = self.mono()
        lst = [t for t in self._fails.get(key, []) if now - t < self.window]
        self._fails[key] = lst
        return lst

    def blocked(self, key):
        with self._lock:
            return len(self._recent(key)) >= self.max

    def fail(self, key):
        with self._lock:
            self._recent(key).append(self.mono())

    def clear(self, key):
        with self._lock:
            self._fails.pop(key, None)
