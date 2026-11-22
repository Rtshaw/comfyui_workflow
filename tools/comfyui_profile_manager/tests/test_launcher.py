"""啟動器與 config 測試（以 --dry-run 執行真實腳本）。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from profile_manager.config import ConfigError, load_profiles

LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "launcher", "comfyui-profile-launch")


def toml_for(py, validated_alt=False):
    p = lambda name, v: (f'[[profile]]\nname="{name}"\ndisplay_name="{name}"\npython={json.dumps(py)}\n'
                         f'app_dir={json.dumps(os.path.dirname(py))}\nargs=["--port","8188"]\n'
                         f'expected_python="3.12"\nexpected_torch="2.14"\nvalidated={str(v).lower()}\n')
    return p("standard", True) + p("alt", validated_alt)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.toml = os.path.join(self.tmp, "profiles.toml")
        self.active = os.path.join(self.tmp, "active")
        with open(self.toml, "w") as f:
            f.write(toml_for(sys.executable))

    def run_launcher(self, name):
        with open(self.active, "w") as f:
            f.write(name)
        return subprocess.run([sys.executable, LAUNCHER, "--dry-run", "--profiles", self.toml,
                               "--active-file", self.active], capture_output=True, text=True)

    def test_approved_profile_ok(self):
        r = self.run_launcher("standard")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = json.loads(r.stdout)["argv"]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[-2:], ["--port", "8188"])

    def test_refuses_unapproved(self):
        for bad in ("alt", "missing", "../../etc/passwd", "UPPER", ""):  # alt 為 validated=false
            r = self.run_launcher(bad)
            self.assertEqual(r.returncode, 2, bad)
            self.assertEqual(r.stdout, "")

    def test_refuses_missing_python(self):
        with open(self.toml, "w") as f:
            f.write(toml_for(os.path.join(self.tmp, "nope", "python")))
        self.assertEqual(self.run_launcher("standard").returncode, 2)


class ConfigTests(unittest.TestCase):
    def test_bad_name_rejected(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "p.toml")
        with open(path, "w") as f:
            f.write(toml_for(sys.executable).replace('name="alt"', 'name="Bad Name"'))
        with self.assertRaises(ConfigError):
            load_profiles(path)

    def test_example_file_valid_and_standard_matches_service(self):
        ex = os.path.join(os.path.dirname(LAUNCHER), "..", "deploy", "profiles.example.toml")
        ps = load_profiles(ex)
        self.assertEqual(ps["standard"].argv(), [
            "/srv/comfyui/venv/bin/python", "/srv/comfyui/app/main.py", "--listen", "127.0.0.1",
            "--port", "8188", "--disable-auto-launch", "--enable-manager"])
        self.assertFalse(ps["trellis2"].validated)


if __name__ == "__main__":
    unittest.main()
