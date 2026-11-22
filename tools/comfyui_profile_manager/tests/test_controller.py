"""controller 切換狀態機測試（離線、假時鐘）。"""
import threading
import unittest

from profile_manager.controller import SwitchRefused
from tests.fakes import make_controller


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.ctl, self.w, self.store = make_controller()

    def run_switch(self, name):
        self.ctl.start_switch(name, background=False)
        return self.ctl.snapshot()

    def test_empty_queue_success(self):
        s = self.run_switch("alt")
        self.assertEqual(s["result"]["outcome"], "SUCCESS")
        self.assertEqual(self.store.get("active"), "alt")
        self.assertEqual(self.store.get("last_good"), "alt")
        self.assertEqual(self.w.running, "alt")

    def test_busy_running_refused(self):
        self.w.queue = ([1], [])
        with self.assertRaises(SwitchRefused):
            self.ctl.start_switch("alt", background=False)
        self.assertEqual(self.w.restarts, [])

    def test_busy_pending_refused(self):
        self.w.queue = ([], [1])
        with self.assertRaises(SwitchRefused):
            self.ctl.start_switch("alt", background=False)
        self.assertEqual(self.w.restarts, [])

    def test_queue_became_busy_before_stop_aborts(self):
        calls = []

        def hook():
            calls.append(1)
            if len(calls) == 1:  # 第一次檢查之後，佇列變忙
                self.w.queue = ([], [1])
        self.w.queue_hook = hook
        s = self.run_switch("alt")
        self.assertEqual(s["result"]["outcome"], "ABORTED")
        self.assertEqual(self.w.restarts, [])
        self.assertEqual(self.store.get("active"), "standard")

    def test_invalid_and_unvalidated_and_missing_python(self):
        for bad in ("../etc", "nope", "raw", "ghost", "standard"):
            with self.assertRaises(SwitchRefused, msg=bad):
                self.ctl.start_switch(bad, background=False)
        self.assertEqual(self.w.restarts, [])

    def test_start_failure_rolls_back(self):
        self.w.fail_start.add("alt")
        s = self.run_switch("alt")
        self.assertEqual(s["result"]["outcome"], "ROLLED_BACK")
        self.assertEqual(self.store.get("active"), "standard")
        self.assertEqual(self.w.running, "standard")

    def test_health_timeout_rolls_back(self):
        self.w.never_healthy.add("alt")
        s = self.run_switch("alt")
        self.assertEqual(s["result"]["outcome"], "ROLLED_BACK")
        self.assertEqual(self.w.running, "standard")

    def test_version_mismatch_rolls_back(self):
        self.w.bad_torch.add("alt")
        self.assertEqual(self.run_switch("alt")["result"]["outcome"], "ROLLED_BACK")

    def test_cmdline_mismatch_rolls_back(self):
        self.w.bad_cmdline.add("alt")
        self.assertEqual(self.run_switch("alt")["result"]["outcome"], "ROLLED_BACK")

    def test_rollback_failure_is_rescue_needed(self):
        self.w.never_healthy.update({"alt", "standard"})
        s = self.run_switch("alt")
        self.assertEqual(s["state"], "RESCUE_NEEDED")
        self.assertEqual(s["result"]["outcome"], "RESCUE_NEEDED")
        self.assertNotIn("已自動回復", s["result"]["reason"])
        with self.assertRaises(SwitchRefused):  # RESCUE_NEEDED 後拒絕再切換
            self.ctl.start_switch("alt", background=False)

    def test_concurrent_switch_refused_by_lock(self):
        gate = threading.Event()
        entered = threading.Event()
        self.w.queue_hook = lambda: (entered.set(), gate.wait(5))
        t = threading.Thread(target=self.ctl.start_switch, args=("alt", False))
        t.start()
        self.assertTrue(entered.wait(5))
        with self.assertRaises(SwitchRefused):
            self.ctl.start_switch("alt", background=False)
        gate.set()
        t.join()


if __name__ == "__main__":
    unittest.main()
