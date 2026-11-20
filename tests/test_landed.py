"""R02 contract: a ready or todo task whose commit is already on the branch is completed by sync."""
from pathlib import Path
import unittest

from test_runner import RunnerTest, card

from dsh_runner import cli


class LandedTest(RunnerTest):
    def land_by_hand(self, subject):
        (self.repo / "src").mkdir(exist_ok=True)
        (self.repo / "src" / "out.txt").write_text("ok")
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", subject)

    def test_ready_task_with_its_commit_on_the_branch_is_completed(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        self.assertEqual(cli.main(["-C", str(self.repo), "sync"]), 0)
        self.assertEqual(self.board()["A01"]["status"], "ready")
        self.land_by_hand("A01: Write ok")
        self.assertEqual(cli.main(["-C", str(self.repo), "sync"]), 0)
        self.assertEqual(self.board()["A01"]["status"], "done")

    def test_todo_chain_landed_by_hand_is_completed_parents_first(self):
        # Hermes refuses to complete a task whose parent is still open, so parents must be completed first.
        self.commit_cards(A01=card("A01", "Write ok"),
                          A02=card("A02", "Write two", depends=("A01",), allowed=("src/two.txt",)),
                          A03=card("A03", "Write three", depends=("A02",), allowed=("src/three.txt",)))
        self.assertEqual(cli.main(["-C", str(self.repo), "sync"]), 0)
        self.assertEqual(self.board()["A03"]["status"], "todo")
        self.land_by_hand("A03: Write three")
        self.land_by_hand("A01: Write ok")
        self.land_by_hand("A02: Write two")
        self.assertEqual(cli.main(["-C", str(self.repo), "sync"]), 0)
        self.assertEqual({c: self.board()[c]["status"] for c in ("A01", "A02", "A03")},
                         {"A01": "done", "A02": "done", "A03": "done"})

    def test_landed_child_of_an_open_parent_stays_open_without_breaking_sync(self):
        self.commit_cards(A01=card("A01", "Write ok"),
                          A02=card("A02", "Write two", depends=("A01",), allowed=("src/two.txt",)))
        self.assertEqual(cli.main(["-C", str(self.repo), "sync"]), 0)
        self.land_by_hand("A02: Write two")
        self.assertEqual(cli.main(["-C", str(self.repo), "sync"]), 0)
        self.assertEqual(self.board()["A01"]["status"], "ready")
        self.assertEqual(self.board()["A02"]["status"], "todo")  # Hermes refuses; nothing crashes or loops

    def test_a_similar_subject_does_not_complete_the_task(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        self.assertEqual(cli.main(["-C", str(self.repo), "sync"]), 0)
        self.land_by_hand("A01: Write ok (partial)")
        self.assertEqual(cli.main(["-C", str(self.repo), "sync"]), 0)
        self.assertEqual(self.board()["A01"]["status"], "ready")

    def test_run_skips_a_card_landed_by_hand_without_a_session(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        self.assertEqual(cli.main(["-C", str(self.repo), "sync"]), 0)
        self.land_by_hand("A01: Write ok")
        self.plan({"write": {"src/out.txt": "WRONG"}})
        self.assertEqual(self.run_driver(), 0)
        self.assertEqual(self.board()["A01"]["status"], "done")
        self.assertEqual(self.git("show", "HEAD:src/out.txt"), "ok")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "A01: Write ok")


class NoLoopTest(RunnerTest):
    """Found overnight 2026-11-19: a satisfied card the board would not complete was rerun every ~70 s for 17 h."""

    def run_with_alarm(self, seconds=60):
        import signal

        def boom(signum, frame):
            raise AssertionError("runner looped")
        old = signal.signal(signal.SIGALRM, boom)
        signal.alarm(seconds)
        try:
            return self.run_driver()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    def test_satisfied_card_the_board_refuses_to_complete_runs_once(self):
        import os
        from unittest.mock import patch
        self.commit_cards(A01=card("A01", "Write ok"))
        (self.repo / "src").mkdir(exist_ok=True)
        (self.repo / "src" / "out.txt").write_text("ok")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "earlier work already satisfies A01")
        self.plan()
        with patch.dict(os.environ, FAKE_HERMES_REFUSE_COMPLETE="1"):
            self.assertEqual(self.run_with_alarm(), 0)
        self.assertNotEqual(self.board()["A01"]["status"], "done")
        self.assertTrue(any("refused to complete" in c for c in self.board()["A01"]["comments"]))
        self.assertFalse(Path(os.environ["FAKE_DSH_PLAN"] + ".count").exists())  # no model session

    def test_committed_card_whose_parent_was_landed_by_hand_completes(self):
        # A parent landed by hand while its task was ready; its dependents must still run and complete.
        self.commit_cards(A01=card("A01", "Write ok"),
                          A02=card("A02", "Write two", depends=("A01",), allowed=("src/two.txt",), expected="two"),
                          A03=card("A03", "Check two", depends=("A02",), allowed=("src/two.txt",), expected="two"))
        self.assertEqual(cli.main(["-C", str(self.repo), "sync"]), 0)
        (self.repo / "src").mkdir(exist_ok=True)
        (self.repo / "src" / "out.txt").write_text("ok")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "A01: Write ok")
        self.plan({"write": {"src/two.txt": "two"}})
        self.assertEqual(self.run_with_alarm(), 0)
        self.assertEqual({c: self.board()[c]["status"] for c in ("A01", "A02", "A03")},
                         {"A01": "done", "A02": "done", "A03": "done"})
        self.assertEqual(self.board()["A03"]["result"], "verified no-change")


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for cls in (LandedTest, NoLoopTest):
        for name in sorted(n for n in vars(cls) if n.startswith("test_")):
            suite.addTest(cls(name))
    return suite


if __name__ == "__main__":
    unittest.main()
