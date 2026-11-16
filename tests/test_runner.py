"""End-to-end: the real runner against a temporary git repo, a fake Hermes board and a scripted fake DSH."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_runner.cards import load_cards, parse_card  # noqa: E402
from dsh_runner.config import load  # noqa: E402
from dsh_runner.driver import Driver  # noqa: E402

FAKES = Path(__file__).resolve().parent / "fakes"
CHECK = "import pathlib, sys\nsys.exit(0 if pathlib.Path('src/out.txt').read_text() == 'ok' else 1)\n"


def card(cid, title, depends=(), gate=False, allowed=("src/out.txt",)):
    return textwrap.dedent(f"""\
        ---
        id: {cid}
        title: {title}
        depends: [{", ".join(depends)}]
        allowed: [{", ".join(allowed)}]
        accept: python3 tools/check.py
        gate: {str(gate).lower()}
        ---
        Write the word ok into src/out.txt.
        """)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsh-runner-test-"))
        self.repo = self.tmp / "repo"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name in ("hermes", "dsh"):
            wrapper = bin_dir / name
            wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKES / (name + '.py')} \"$@\"\n")
            wrapper.chmod(0o755)
        env = patch.dict(os.environ, FAKE_HERMES_DB=str(self.tmp / "board.json"),
                         FAKE_DSH_PLAN=str(self.tmp / "plan.json"), DSH_RUNNER_HERMES=str(bin_dir / "hermes"))
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.repo / "docs" / "cards").mkdir(parents=True)
        (self.tmp / "dsh-home").mkdir()
        (self.repo / "tools").mkdir()
        (self.repo / "tools" / "check.py").write_text(CHECK)
        (self.repo / "README.md").write_text("original\n")
        (self.repo / ".gitignore").write_text("runs/\n")
        (self.repo / "dsh-runner.toml").write_text(textwrap.dedent(f"""\
            [runner]
            board = "test-board"
            worker_rules = ["Keep changes small."]
            [dsh]
            home = "{self.tmp / 'dsh-home'}"
            bin = "{bin_dir / 'dsh'}"
            [limits]
            run_timeout_min = 1
            attempts = 2
            infra_retries = 1
            infra_backoff_s = 0
            cooldown_s = 0
            min_nudge_s = 3600
            poll_s = 0.05
            pause_file = "{self.tmp / 'PAUSE'}"
            model_lock = "{self.tmp / 'model.lock'}"
            """))

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True,
                              check=True).stdout.strip()

    def commit_cards(self, **cards):
        for name, text in cards.items():
            (self.repo / "docs" / "cards" / f"{name}.md").write_text(text)
        if not (self.repo / ".git").exists():
            self.git("init", "-q", "-b", "main")
            self.git("config", "user.email", "runner@example.invalid")
            self.git("config", "user.name", "runner test")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "cards")

    def plan(self, *steps):
        Path(os.environ["FAKE_DSH_PLAN"]).write_text(json.dumps(list(steps)))

    def run_driver(self, **kwargs):
        return Driver(load(self.repo)).run(**kwargs)

    def board(self):
        return {t["title"].split(":")[0]: t for t in json.loads(Path(os.environ["FAKE_HERMES_DB"]).read_text())["tasks"].values()}

    def prompt(self, index):
        return Path(os.environ["FAKE_DSH_PLAN"] + f".prompt{index}").read_text()

    def test_passing_card_is_committed_and_completed(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        self.plan({"write": {"src/out.txt": "ok"}})
        self.assertEqual(self.run_driver(), 0)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "A01: Write ok")
        self.assertEqual(self.git("show", "HEAD:src/out.txt"), "ok")
        self.assertEqual(self.board()["A01"]["status"], "done")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn("Edit ONLY these files: src/out.txt", self.prompt(0))
        self.assertIn("- Keep changes small.", self.prompt(0))

    def test_python_bytecode_is_not_an_out_of_scope_edit(self):
        # Found by a real smoke run: a model running the tests left src/__pycache__/*.pyc behind.
        self.commit_cards(A01=card("A01", "Write ok"))
        self.plan({"write": {"src/out.txt": "ok", "src/__pycache__/out.cpython-312.pyc": "bytecode"}})
        self.assertEqual(self.run_driver(), 0)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "A01: Write ok")
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD"), "src/out.txt")

    def test_session_cut_off_at_the_token_limit_is_resumed_not_failed(self):
        # Found in a real run: a model read for a whole turn, hit max-tokens before writing, and lost the attempt.
        self.commit_cards(A01=card("A01", "Write ok"))
        config = self.repo / "dsh-runner.toml"
        config.write_text(config.read_text().replace("min_nudge_s = 3600", "min_nudge_s = 1"))
        self.git("commit", "-qam", "nudge sooner")
        self.plan({"truncate": True}, {"write": {"src/out.txt": "ok"}})
        self.assertEqual(self.run_driver(), 0)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "A01: Write ok")
        self.assertIn("output-token limit", self.prompt(1))
        result = json.loads((self.repo / "runs/board/A01/attempt-1/result.json").read_text())
        self.assertEqual((result["attempt"], result["nudges"]), (1, 1))

    def test_out_of_scope_edits_are_reverted_and_never_committed(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        self.plan({"write": {"src/out.txt": "ok", "README.md": "tampered\n", "src/extra.txt": "x"}},
                  {"write": {"src/out.txt": "ok"}})
        self.assertEqual(self.run_driver(), 0)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "A01: Write ok")
        self.assertEqual(self.git("show", "HEAD:README.md"), "original")
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD"), "src/out.txt")
        self.assertFalse((self.repo / "src" / "extra.txt").exists())
        self.assertIn("outside the allowed list", self.prompt(1))

    def test_failing_card_is_blocked_with_a_clean_tree_and_carried_work(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        self.plan({"write": {"src/out.txt": "no"}}, {"write": {"src/out.txt": "still no"}})
        self.assertEqual(self.run_driver(), 0)
        self.assertEqual(self.board()["A01"]["status"], "blocked")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "cards")
        self.assertEqual(self.git("status", "--porcelain"), "")
        carried = self.repo / "runs" / "board" / "A01" / "carry" / "files" / "src" / "out.txt"
        self.assertEqual(carried.read_text(), "still no")
        self.assertIn("Acceptance failed", self.prompt(1))

    def test_dependencies_run_in_order_and_gates_hold_only_themselves(self):
        self.commit_cards(A01=card("A01", "First"), A02=card("A02", "Gate", depends=["A01"], gate=True),
                          A03=card("A03", "Second", depends=["A01"], allowed=["src/out.txt", "src/second.txt"]))
        self.plan({"write": {"src/out.txt": "ok"}}, {"write": {"src/second.txt": "2"}})
        self.assertEqual(self.run_driver(), 0)
        self.assertEqual(self.git("log", "--format=%s", "-2").splitlines(), ["A03: Second", "A01: First"])
        self.assertNotEqual(self.board()["A02"]["status"], "done")

    def test_unreachable_model_releases_the_card_without_burning_attempts(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        self.plan({"silent_fail": True}, {"silent_fail": True})
        self.assertEqual(self.run_driver(), 3)
        self.assertEqual(self.board()["A01"]["status"], "ready")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "cards")

    def test_a_dsh_that_cannot_launch_fails_fast_without_retries(self):
        # Found in a real run: node missing from PATH looked like an outage and was retried with backoff.
        self.commit_cards(A01=card("A01", "Write ok"))
        self.plan({"write": {"src/out.txt": "ok"}})
        with patch.dict(os.environ, FAKE_DSH_BROKEN="1"):
            self.assertEqual(self.run_driver(), 1)
        self.assertEqual(self.board()["A01"]["status"], "ready")
        self.assertTrue(any("node: No such file" in c for c in self.board()["A01"]["comments"]))
        self.assertEqual(len(list((self.repo / "runs/board/A01").glob("attempt-*"))), 1)

    def test_check_launches_dsh(self):
        from dsh_runner import cli
        self.commit_cards(A01=card("A01", "Write ok"))
        self.assertEqual(cli.main(["-C", str(self.repo), "check"]), 0)
        with patch.dict(os.environ, FAKE_DSH_BROKEN="1"):
            self.assertEqual(cli.main(["-C", str(self.repo), "check"]), 1)

    def test_stop_file_prevents_any_session(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        (self.repo / "runs" / "board").mkdir(parents=True)
        (self.repo / "runs" / "board" / "STOP").touch()
        self.plan({"write": {"src/out.txt": "ok"}})
        self.assertEqual(self.run_driver(), 0)
        self.assertFalse(Path(os.environ["FAKE_DSH_PLAN"] + ".count").exists())

    def test_dirty_tree_is_refused(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        (self.repo / "README.md").write_text("local edit\n")
        with self.assertRaises(SystemExit):
            self.run_driver()

    def hold(self, path):
        """Hold a flock on path for the rest of the test, like a live runner in another process."""
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a")
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def add_slots(self):
        config = self.repo / "dsh-runner.toml"
        config.write_text(config.read_text() + textwrap.dedent(f"""\
            [[slots]]
            lock = "{self.tmp / 'slot1.lock'}"
            [[slots]]
            lock = "{self.tmp / 'slot2.lock'}"
            home = "{self.tmp / 'dsh-home-2'}"
            """))

    def test_slots_parse_and_default_to_the_single_model_lock(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        legacy = load(self.repo).model_slots()
        self.assertEqual([(s.lock.name, s.home.name) for s in legacy], [("model.lock", "dsh-home")])
        self.add_slots()
        slots = load(self.repo).model_slots()
        self.assertEqual([(s.lock.name, s.home.name) for s in slots],
                         [("slot1.lock", "dsh-home"), ("slot2.lock", "dsh-home-2")])
        config = self.repo / "dsh-runner.toml"
        config.write_text(config.read_text() + '[[slots]]\nlock = "x"\nhost = "y"\n')
        with self.assertRaisesRegex(SystemExit, "slots.host"):
            load(self.repo)

    def test_a_busy_slot_sends_the_session_to_the_next_one(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        self.add_slots()
        self.git("commit", "-qam", "two slots")
        self.hold(self.tmp / "slot1.lock")
        self.plan({"write": {"src/out.txt": "ok"}})
        self.assertEqual(self.run_driver(), 0)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "A01: Write ok")
        self.assertEqual(Path(os.environ["FAKE_DSH_PLAN"] + ".home0").read_text(), str(self.tmp / "dsh-home-2"))

    def test_pause_while_every_slot_is_busy_claims_nothing(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        self.hold(self.tmp / "model.lock")
        driver = Driver(load(self.repo))
        (self.tmp / "PAUSE").touch()
        with driver.model_slot() as slot:
            self.assertIsNone(slot)
        self.assertEqual(driver.config.dsh_home, driver.base.dsh_home)

    def test_a_card_another_live_runner_owns_is_skipped(self):
        self.commit_cards(A01=card("A01", "First"), A02=card("A02", "Second", allowed=["src/second.txt"]))
        driver = Driver(load(self.repo))
        driver.sync_board(load_cards(driver.config.cards_path, driver.config.card_pattern))
        driver.board.claim(self.board()["A01"]["id"], 60)
        self.hold(driver.config.claims_path / "A01.lock")
        self.plan({"write": {"src/second.txt": "2"}})
        check2 = "import pathlib, sys\nsys.exit(0 if pathlib.Path('src/second.txt').exists() else 1)\n"
        (self.repo / "tools" / "check.py").write_text(check2)
        self.git("commit", "-qam", "check second")
        self.assertEqual(driver.run(once=True), 0)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "A02: Second")
        self.assertEqual(self.board()["A01"]["status"], "running")
        self.assertEqual(Path(os.environ["FAKE_DSH_PLAN"] + ".count").read_text(), "1")

    def test_a_claim_left_by_a_dead_runner_is_taken_over(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        driver = Driver(load(self.repo))
        driver.sync_board(load_cards(driver.config.cards_path, driver.config.card_pattern))
        driver.board.claim(self.board()["A01"]["id"], 60)  # its runner then died: no card lock is held
        self.plan({"write": {"src/out.txt": "ok"}})
        self.assertEqual(driver.run(), 0)
        self.assertEqual(self.board()["A01"]["status"], "done")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "A01: Write ok")

    def test_a_dependency_committed_elsewhere_waits_for_the_merge(self):
        self.commit_cards(A01=card("A01", "First"), A02=card("A02", "Second", depends=["A01"]))
        driver = Driver(load(self.repo))
        driver.sync_board(load_cards(driver.config.cards_path, driver.config.card_pattern))
        a01 = self.board()["A01"]["id"]
        driver.board.claim(a01, 60)
        driver.board.complete(a01, "commit abc1234", "done in another worktree")
        self.plan({"write": {"src/out.txt": "ok"}})
        self.assertEqual(driver.run(), 0)
        self.assertFalse(Path(os.environ["FAKE_DSH_PLAN"] + ".count").exists())
        # Completed by hand (no runner commit): the dependent card runs.
        db = json.loads(Path(os.environ["FAKE_HERMES_DB"]).read_text())
        db["tasks"][a01]["result"] = "done by hand"
        Path(os.environ["FAKE_HERMES_DB"]).write_text(json.dumps(db))
        self.assertEqual(driver.run(), 0)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "A02: Second")

    def test_sync_is_idempotent(self):
        self.commit_cards(A01=card("A01", "First"), A02=card("A02", "Second", depends=["A01"]))
        driver = Driver(load(self.repo))
        cards = load_cards(driver.config.cards_path, driver.config.card_pattern)
        driver.sync_board(cards)
        driver.sync_board(cards)
        board = self.board()
        self.assertEqual(sorted(board), ["A01", "A02"])
        self.assertEqual(board["A02"]["parents"], [board["A01"]["id"]])

    def test_unknown_config_keys_are_errors(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        config = self.repo / "dsh-runner.toml"
        config.write_text(config.read_text().replace("run_timeout_min", "run_timout_min"))
        with self.assertRaisesRegex(SystemExit, "limits.run_timout_min"):
            load(self.repo)

    def test_card_header_is_validated(self):
        path = self.tmp / "bad.md"
        path.write_text("---\nid: A01\ntitle: no accept\nallowed: [x]\n---\nbody\n")
        with self.assertRaises(ValueError):
            parse_card(path)


if __name__ == "__main__":
    unittest.main()
