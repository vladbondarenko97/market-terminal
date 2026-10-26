"""scripts/deploy.sh against throwaway git repos, with stub gh / launchctl / curl / sleep (no network, no real services)."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "deploy.sh"
STUBS = {
    "gh": 'echo "$CI_RESULT"',
    "launchctl": 'echo "$@" >> "$STUB_LOG"',
    "curl": '[ -f "$HEALTH_FILE" ]',
    "sleep": "true",
}


class Deploy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", "origin.git")
        self.git(self.tmp, "clone", "-q", "origin.git", "live")
        self.live = self.tmp / "live"
        (self.live / "scripts").mkdir()
        shutil.copy(SCRIPT, self.live / "scripts" / "deploy.sh")
        self.commit(self.live, "a.txt", "1")
        self.git(self.live, "push", "-q", "origin", "main")
        self.git(self.tmp, "clone", "-q", "origin.git", "dev")      # where "the developer" merges
        self.dev = self.tmp / "dev"
        bins = self.tmp / "bin"
        bins.mkdir()
        for name, body in STUBS.items():
            (bins / name).write_text("#!/bin/sh\n" + body + "\n")
            (bins / name).chmod(0o755)
        self.env = {**os.environ, "PATH": f"{bins}:{os.environ['PATH']}", "CI_RESULT": "completed/success",
                    "STUB_LOG": str(self.tmp / "launchctl.log"), "HEALTH_FILE": str(self.tmp / "healthy")}
        (self.tmp / "healthy").write_text("")

    def git(self, cwd, *args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, repo, name, text):
        (repo / name).write_text(text)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", text)

    def merge_upstream(self, text="2"):
        self.git(self.dev, "pull", "-q")
        self.commit(self.dev, "a.txt", text)
        self.git(self.dev, "push", "-q", "origin", "main")

    def deploy(self, *args, **env):
        r = subprocess.run(["bash", "scripts/deploy.sh", *args], cwd=self.live, env={**self.env, **env}, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def restarts(self):
        log = self.tmp / "launchctl.log"
        return len(log.read_text().splitlines()) if log.exists() else 0

    def head(self):
        return self.git(self.live, "rev-parse", "HEAD")

    def test_green_commit_is_deployed_once(self):
        self.merge_upstream()
        code, out = self.deploy()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.live / "a.txt").read_text(), "2")
        self.assertEqual(self.restarts(), 2)                       # terminal + MCP server
        self.assertEqual(self.deploy()[1].strip(), f"deploy: up to date ({self.head()[:7]})")
        self.assertEqual(self.restarts(), 2)

    def test_red_or_missing_ci_changes_nothing(self):
        self.merge_upstream()
        before = self.head()
        for result in ("completed/failure", "in_progress/", ""):
            code, out = self.deploy(CI_RESULT=result)
            self.assertEqual(code, 1, out)
        self.assertEqual((self.head(), self.restarts()), (before, 0))

    def test_hand_pull_still_gets_restarted(self):
        self.deploy()                                              # first run records the commit the services run
        self.merge_upstream()
        self.git(self.live, "pull", "-q")                          # someone pulled by hand: HEAD == origin/main, services stale
        code, out = self.deploy()
        self.assertEqual((code, self.restarts()), (0, 2), out)
        self.assertIn("deployed", out)

    def test_dirty_checkout_is_left_alone(self):
        self.merge_upstream()
        (self.live / "a.txt").write_text("local edit")
        code, out = self.deploy()
        self.assertEqual(code, 1, out)
        self.assertEqual(self.restarts(), 0)

    def test_dead_terminal_rolls_back(self):
        self.deploy()
        good = self.head()
        self.merge_upstream("broken")
        (self.tmp / "healthy").unlink()
        code, out = self.deploy()
        self.assertEqual(code, 1, out)
        self.assertIn("rolling back", out)
        self.assertEqual((self.head(), (self.live / "a.txt").read_text()), (good, "1"))

    def test_failed_commit_is_not_retried_and_hand_pull_rolls_back_to_the_running_commit(self):
        self.deploy()
        running = self.head()
        self.merge_upstream("c2")
        self.git(self.live, "pull", "-q")                          # hand pull: files at c2, services still on the stamped commit
        self.merge_upstream("c3")
        (self.tmp / "healthy").unlink()
        self.assertEqual(self.deploy()[0], 1)
        self.assertEqual(self.head(), running)                     # back on what was verified, not the hand-pulled c2
        before = self.restarts()
        code, out = self.deploy()
        self.assertEqual((code, self.restarts()), (1, before), out)   # same bad commit: no restart loop
        self.assertIn("already failed", out)

    def test_first_run_and_garbage_stamp_do_not_restart_a_current_checkout(self):
        self.assertIn("up to date", self.deploy()[1])              # no stamp yet
        self.assertEqual(self.restarts(), 0)
        self.merge_upstream()
        (self.live / ".git" / "deployed-sha").write_text("0" * 40)  # rewritten history
        code, out = self.deploy()
        self.assertEqual((code, self.restarts()), (0, 2), out)

    def test_check_changes_nothing(self):
        self.merge_upstream()
        before = self.head()
        code, out = self.deploy("--check")
        self.assertEqual((code, self.head(), self.restarts()), (0, before, 0), out)


if __name__ == "__main__":
    unittest.main()
