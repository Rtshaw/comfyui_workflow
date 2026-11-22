"""讀取並驗證 profiles.toml（只用標準函式庫 tomllib）。

輸入：TOML 檔，內含多個 [[profile]] 表。
輸出：name -> Profile 的 dict。
錯誤：任何欄位缺漏、型別錯誤、名稱不合白名單或重複，一律丟 ConfigError。
"""
import re
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")  # profile 名稱白名單


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    display_name: str
    python: str
    app_dir: str
    args: tuple
    expected_python: str  # 版本前綴比對
    expected_torch: str
    validated: bool  # 由管理者人工實測後手動設 true

    def argv(self):
        """啟動 ComfyUI 的完整 argv；argv[0] 即 python，供 /proc cmdline 比對。"""
        return [self.python, self.app_dir.rstrip("/\\") + "/main.py", *self.args]


def is_abs(p):
    # 部署在 Linux；同時接受 Windows 路徑只是為了讓本機測試可跑
    return PurePosixPath(p).is_absolute() or PureWindowsPath(p).is_absolute()


def _parse(raw):
    def need(key, typ):
        v = raw.get(key)
        if not isinstance(v, typ) or (typ is str and not v):
            raise ConfigError(f"profile {raw.get('name')!r}: 欄位 {key} 缺漏或型別錯誤")
        return v

    name = need("name", str)
    if not NAME_RE.match(name):
        raise ConfigError(f"profile 名稱不合法：{name!r}")
    args = need("args", list)
    if not all(isinstance(a, str) for a in args):
        raise ConfigError(f"profile {name}: args 必須是字串清單")
    python, app_dir = need("python", str), need("app_dir", str)
    if not (is_abs(python) and is_abs(app_dir)):
        raise ConfigError(f"profile {name}: python 與 app_dir 必須是絕對路徑")
    return Profile(name, need("display_name", str), python, app_dir, tuple(args),
                   need("expected_python", str), need("expected_torch", str),
                   need("validated", bool))


def load_profiles(path):
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"無法讀取 {path}: {e}")
    profiles = {}
    for raw in data.get("profile", []):
        p = _parse(raw)
        if p.name in profiles:
            raise ConfigError(f"profile 名稱重複：{p.name}")
        profiles[p.name] = p
    if not profiles:
        raise ConfigError("profiles.toml 沒有任何 [[profile]]")
    return profiles
