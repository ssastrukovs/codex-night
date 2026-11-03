import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("night", HERE / "codex_night.py")
night = importlib.util.module_from_spec(spec)
spec.loader.exec_module(night)
ID = "0199a213-81c0-7800-8aa1-bbab2a035a53"
OTHER = "0199a213-81c0-7800-8aa1-bbab2a035a54"
FAKE = r'''#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
p=Path(os.environ['FAKE_CALLS'])
calls=json.loads(p.read_text()) if p.exists() else []
previous_alive=False
if calls:
    try:os.kill(calls[-1]['pid'],0);previous_alive=True
    except ProcessLookupError:pass
prompt=sys.stdin.read()
calls.append({'argv':sys.argv[1:],'prompt':prompt,'cwd':os.getcwd(),'pid':os.getpid(),'previous_alive':previous_alive})
p.write_text(json.dumps(calls))
plan=json.loads(os.environ['FAKE_PLAN'])
mode=plan[min(len(calls)-1,len(plan)-1)]
identity=os.environ['FAKE_ID']
def emit(x):print(json.dumps(x),flush=True)
def error(message,code=None):
    emit({'type':'turn.failed','error':{'message':message,'codexErrorInfo':code}})
if mode=='before_thread':
    print('Error: The model is at capacity. Please try again later.',file=sys.stderr,flush=True)
    sys.exit(1)
if mode!='no_id':emit({'type':'thread.started','thread_id':identity})
if mode=='thread_only':
    emit({'type':'error','message':'The model is at capacity'})
    sys.exit(1)
emit({'type':'turn.started'})
if mode=='hold':time.sleep(60)
if mode=='huge_stderr':sys.stderr.write('ж'*300000);sys.stderr.flush()
if mode in ('capacity','capacity_zero','no_id','retry_after'):
    e={'message':'The model is at capacity','codexErrorInfo':'serverOverloaded'}
    if mode=='retry_after':e['retry_after']=0.03
    emit({'type':'error','message':e['message']})
    emit({'type':'turn.failed','error':e})
    sys.exit(0 if mode=='capacity_zero' else 1)
if mode=='quota':error('Usage limit exceeded.','usageLimitExceeded');sys.exit(1)
if mode=='auth':error('Unauthorized','unauthorized');sys.exit(1)
if mode=='other_after_capacity':
    emit({'type':'error','message':'The model is at capacity'})
    error('Invalid request: bad schema','badRequest');sys.exit(2)
if mode=='missing_terminal':sys.exit(0)
emit({'type':'item.completed','item':{'type':'agent_message','text':'This document mentions model is at capacity; job done.'}})
emit({'type':'turn.completed','usage':{}})
'''


class ClassifierTests(unittest.TestCase):
    def test_capacity_and_structured_code(self):
        for error in ("The model is at capacity. Please try again later.",
                      "The model is temporarily overloaded.", {"codexErrorInfo": "serverOverloaded"},
                      {"code": "server_is_overloaded"}):
            with self.subTest(error=error):
                self.assertTrue(night.is_capacity(error))

    def test_auth_quota_context_and_approval_are_not_capacity(self):
        for error in ("Usage limit exceeded", "429 rate limit exceeded", "insufficient_quota",
                      "permission denied", "Invalid API key", "Context window exceeded",
                      "Unauthorized: upstream server overloaded", "sessionBudgetExceeded"):
            with self.subTest(error=error):
                self.assertFalse(night.is_capacity(error))

    def test_retry_after_and_signal_failure(self):
        self.assertEqual(night.retry_after({"details": "Retry-After: 300"}), 300)
        self.assertEqual(night.retry_after({"retry_after": float("nan")}), 0)
        o = night.Outcome(ID)
        o.event({"type": "turn.failed", "error": "The model is at capacity"})
        for code in (-2, 130, 143):
            self.assertIsNone(o.failure(code, ""))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cwd = self.root / "workspace with spaces $x `literal`"
        self.cwd.mkdir()
        self.fake = self.root / "fake-codex"
        self.fake.write_text(FAKE)
        self.fake.chmod(0o700)
        self.state = self.root / "state"
        self.calls_file = self.root / "calls.json"

    def tearDown(self):
        self.temp.cleanup()

    def command(self, *extra, prompt="Original task; $(touch should-not-exist)"):
        return [sys.executable, str(HERE / "codex_night.py"), "--codex-bin", str(self.fake),
                "--state-dir", str(self.state), "-C", str(self.cwd), "--initial-delay", "0.005",
                "--max-delay", "0.02", "--jitter", "0", *extra, prompt]

    def env(self, plan, identity=ID):
        return dict(os.environ, FAKE_CALLS=str(self.calls_file), FAKE_PLAN=json.dumps(plan), FAKE_ID=identity)

    def calls(self):
        return json.loads(self.calls_file.read_text())

    def status(self):
        return json.loads(Path(json.loads((self.state / "latest.json").read_text())["status_file"]).read_text())

    def run_job(self, plan, *extra, expected=0, identity=ID):
        p = subprocess.run(self.command(*extra), env=self.env(plan, identity), capture_output=True, text=True, timeout=8)
        self.assertEqual(p.returncode, expected, p.stderr[-2000:])
        return p

    def test_capacity_resumes_exact_id_and_forwards_settings(self):
        self.run_job(["capacity", "capacity", "success"], "--approve-for-me", "-s", "workspace-write",
                     "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="xhigh"')
        calls = self.calls()
        self.assertEqual(len(calls), 3)
        self.assertNotIn("resume", calls[0]["argv"])
        for c in calls[1:]:
            self.assertEqual(c["argv"][-3:], ["resume", ID, "-"])
            self.assertIn("Продолжай прежнюю задачу", c["prompt"])
            self.assertIn("gpt-6.1-sol", c["argv"])
            self.assertIn("--approve-for-me", c["argv"])
            self.assertIn('model_reasoning_effort="xhigh"', c["argv"])
            self.assertFalse(c["previous_alive"])
            self.assertEqual(c["cwd"], str(self.cwd))
        self.assertFalse((self.cwd / "should-not-exist").exists())
        self.assertEqual(self.status()["session_id"], ID)
        self.assertEqual(self.status()["status"], "completed")

    def test_success_mentioning_capacity_does_not_repeat(self):
        self.run_job(["success"])
        self.assertEqual(len(self.calls()), 1)

    def test_permanent_error_and_stale_capacity_do_not_repeat(self):
        for mode, code in (("quota", 1), ("auth", 1), ("other_after_capacity", 2)):
            with self.subTest(mode=mode):
                if self.calls_file.exists():
                    self.calls_file.unlink()
                self.run_job([mode], expected=code)
                self.assertEqual(len(self.calls()), 1)

    def test_progress_without_id_is_never_replayed(self):
        self.run_job(["no_id"], expected=1)
        self.assertEqual(len(self.calls()), 1)

    def test_failure_before_thread_can_retry_initial_instruction(self):
        self.run_job(["before_thread", "success"])
        first, second = self.calls()
        self.assertEqual(first["prompt"], second["prompt"])
        self.assertNotIn("resume", second["argv"])

    def test_thread_before_turn_keeps_instruction_but_resumes_id(self):
        self.run_job(["thread_only", "success"])
        first, second = self.calls()
        self.assertEqual(first["prompt"], second["prompt"])
        self.assertEqual(second["argv"][-3:], ["resume", ID, "-"])

    def test_zero_exit_with_failed_turn_is_not_success(self):
        self.run_job(["capacity_zero", "success"])
        self.assertEqual(len(self.calls()), 2)

    def test_zero_exit_without_terminal_is_not_success(self):
        self.run_job(["missing_terminal"], expected=1)
        self.assertEqual(self.status()["status"], "failed")

    def test_retry_limit(self):
        self.run_job(["capacity"], "--max-retries", "1", expected=75)
        self.assertEqual(len(self.calls()), 2)

    def test_retry_after_is_honored(self):
        p = self.run_job(["retry_after", "success"], "--json")
        retry = [json.loads(line) for line in p.stdout.splitlines() if json.loads(line)["type"] == "capacity.retry"][0]
        self.assertGreaterEqual(retry["delay_seconds"], 0.03)

    def test_existing_resume(self):
        self.run_job(["success"], "--resume", ID)
        self.assertEqual(self.calls()[0]["argv"][-3:], ["resume", ID, "-"])

    def test_json_logs_are_private(self):
        self.run_job(["success"])
        folder = Path(self.status()["log_dir"])
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in folder.iterdir()))

    def test_stderr_is_drained_while_stdout_is_read(self):
        self.run_job(["huge_stderr"])
        self.assertEqual(self.status()["status"], "completed")

    def wait_status(self, status, session=None):
        end = time.monotonic() + 4
        while time.monotonic() < end:
            try:
                value = self.status()
                if value["status"] == status and self.calls_file.exists() and (session is None or value["session_id"] == session):
                    return
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            time.sleep(0.01)
        self.fail("did not reach " + status)

    def test_interrupt_during_capacity_wait_does_not_retry(self):
        p = subprocess.Popen(self.command("--initial-delay", "60", "--max-delay", "60"),
                             env=self.env(["capacity"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.wait_status("waiting_capacity")
            p.send_signal(signal.SIGINT)
            _, stderr = p.communicate(timeout=5)
            self.assertEqual(p.returncode, 130, stderr.decode())
            self.assertEqual(len(self.calls()), 1)
            self.assertEqual(self.status()["status"], "interrupted")
        finally:
            if p.poll() is None:
                p.kill();p.communicate()

    def test_one_controller_per_workspace_and_child_stops_with_it(self):
        p = subprocess.Popen(self.command(), env=self.env(["hold"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.wait_status("running", ID)
            child_pid = self.calls()[0]["pid"]
            other = subprocess.run(self.command(), env=self.env(["hold"]), capture_output=True, timeout=4)
            self.assertEqual(other.returncode, 1)
            self.assertIn(b"already owns", other.stderr)
            p.send_signal(signal.SIGINT)
            p.communicate(timeout=5)
            self.assertEqual(p.returncode, 130)
            self.assertEqual(self.status()["session_id"], ID)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if p.poll() is None:
                p.kill();p.communicate()

    def test_same_session_cannot_run_from_another_workspace(self):
        p = subprocess.Popen(self.command("--resume", ID), env=self.env(["hold"]),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.wait_status("running", ID)
            other_cwd = self.root / "another-workspace"
            other_cwd.mkdir()
            other = subprocess.run(self.command("--resume", ID, "-C", str(other_cwd)),
                                   env=self.env(["hold"]), capture_output=True, timeout=4)
            self.assertEqual(other.returncode, 1)
            self.assertIn(b"already owns this session", other.stderr)
            self.assertEqual(len(self.calls()), 1)
        finally:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
            p.communicate(timeout=5)

    def test_changed_session_id_is_rejected(self):
        self.run_job(["success"], "--resume", ID, expected=1, identity=OTHER)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.status()["status"], "controller_error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
