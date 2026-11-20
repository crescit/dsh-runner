"""R01 contract: `[dsh] patch` passes a repository patch to DSH and reports the model it names."""
import json
import os
from pathlib import Path
import textwrap
import unittest

from test_runner import RunnerTest, card

from dsh_runner import cli
from dsh_runner.config import load

PATCH = textwrap.dedent("""\
    # repository overlay
    - id: agent-default-model
      config:
        provider: local
        model: tiny-model-1
        reasoningEffort: low
    """)


class PatchTest(RunnerTest):
    def use_patch(self, value="docs/model.patch.yml", write=True):
        if write:
            (self.repo / "docs" / "model.patch.yml").write_text(PATCH)
        config = self.repo / "dsh-runner.toml"
        config.write_text(config.read_text().replace("[dsh]\n", f'[dsh]\npatch = "{value}"\n'))

    def argv(self, index):
        return json.loads(Path(os.environ["FAKE_DSH_PLAN"] + f".argv{index}").read_text())

    def test_patch_is_passed_to_dsh_and_its_model_is_reported(self):
        self.use_patch()
        self.commit_cards(A01=card("A01", "Write ok"))
        self.plan({"write": {"src/out.txt": "ok"}})
        self.assertEqual(self.run_driver(), 0)
        argv = self.argv(0)
        patch_file = str((self.repo / "docs" / "model.patch.yml").resolve())
        self.assertEqual(argv[:4], ["--profile", "headless", "--patch", patch_file])
        self.assertIn("--json", argv)
        self.assertEqual(argv[-1], "-")
        self.assertNotIn("headless", argv[4:])
        message = self.git("log", "-1", "--format=%B")
        self.assertIn("Written by tiny-model-1 (DSH headless, effort low)", message)
        result = json.loads((self.repo / "runs/board/A01/attempt-1/result.json").read_text())
        self.assertEqual((result["model"], result["effort"]), ("tiny-model-1", "low"))

    def test_without_a_patch_the_command_is_unchanged(self):
        self.commit_cards(A01=card("A01", "Write ok"))
        self.plan({"write": {"src/out.txt": "ok"}})
        self.assertEqual(self.run_driver(), 0)
        self.assertEqual(self.argv(0), ["headless", "--json", "-"])
        self.assertIsNone(load(self.repo).dsh_patch)

    def test_absolute_patch_path(self):
        outside = self.tmp / "elsewhere.patch.yml"
        outside.write_text(PATCH.replace("tiny-model-1", "abs-model"))
        self.use_patch(str(outside), write=False)
        self.commit_cards(A01=card("A01", "Write ok"))
        self.assertEqual(load(self.repo).dsh_patch, outside.resolve())

    def test_missing_patch_file_is_a_config_error(self):
        self.use_patch("docs/nope.patch.yml", write=False)
        self.commit_cards(A01=card("A01", "Write ok"))
        with self.assertRaisesRegex(SystemExit, "nope.patch.yml"):
            load(self.repo)

    def test_check_reports_the_patch_model(self):
        import contextlib
        import io
        self.use_patch()
        self.commit_cards(A01=card("A01", "Write ok"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["-C", str(self.repo), "check"]), 0)
        self.assertIn("model    tiny-model-1", out.getvalue())
        self.assertIn("model.patch.yml", out.getvalue())


def load_tests(loader, tests, pattern):
    """Run only the tests defined here, not the inherited RunnerTest ones."""
    suite = unittest.TestSuite()
    for name in sorted(n for n in vars(PatchTest) if n.startswith("test_")):
        suite.addTest(PatchTest(name))
    return suite


if __name__ == "__main__":
    unittest.main()
