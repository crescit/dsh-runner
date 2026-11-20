"""R02 contract: a ready or todo task whose commit is already on the branch is completed by sync."""
import unittest

from test_runner import RunnerTest, card

from dsh_runner import cli


class LandedTest(RunnerTest):
    def land_by_hand(self, subject):
        (self.repo / "src").mkdir(exist_ok=True)
        (self.repo / "src" / "out.txt").write_text("ok")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", subject)

    def test_ready_task_with_its_commit_on_the_branch_is_completed(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        self.assertEqual(cli.main(["-C", str(self.repo), "sync"]), 0)
        self.assertEqual(self.board()["A01"]["status"], "ready")
        self.land_by_hand("A01: Write ok")
        self.assertEqual(cli.main(["-C", str(self.repo), "sync"]), 0)
        self.assertEqual(self.board()["A01"]["status"], "done")

    def test_todo_task_with_its_commit_on_the_branch_is_completed_and_unblocks_children(self):
        self.commit_cards(A01=card("A01", "Write ok"),
                          A02=card("A02", "Write two", depends=("A01",), allowed=("src/two.txt",)))
        self.assertEqual(cli.main(["-C", str(self.repo), "sync"]), 0)
        self.assertEqual(self.board()["A02"]["status"], "todo")
        self.land_by_hand("A02: Write two")
        self.assertEqual(cli.main(["-C", str(self.repo), "sync"]), 0)
        self.assertEqual(self.board()["A02"]["status"], "done")
        self.assertEqual(self.board()["A01"]["status"], "ready")

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


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in sorted(n for n in vars(LandedTest) if n.startswith("test_")):
        suite.addTest(LandedTest(name))
    return suite


if __name__ == "__main__":
    unittest.main()
