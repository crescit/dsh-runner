"""Fake `dsh headless --json -`: replays scripted worker sessions from $FAKE_DSH_PLAN, one per invocation.

Each plan step is a dict:
    {"write": {"relative/path": "content"}}   files to write in the repository (cwd)
    {"silent_fail": true}                     exit 1 with no output, like an unreachable model server
    {"truncate": true}                        read a file, then end the turn at the output-token limit (exit 1)
Invocations beyond the plan behave like a session that only reads. `--version` prints a version, and with
$FAKE_DSH_BROKEN set every launch fails like a missing interpreter (exit 127).
"""
import json
import os
from pathlib import Path
import sys

if os.environ.get("FAKE_DSH_BROKEN"):
    print("env: node: No such file or directory", file=sys.stderr)
    sys.exit(127)
if sys.argv[1:] == ["--version"]:
    print("0.0.0-fake")
    sys.exit(0)

plan = json.loads(Path(os.environ["FAKE_DSH_PLAN"]).read_text())
counter = Path(os.environ["FAKE_DSH_PLAN"] + ".count")
index = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(index + 1))
prompt = sys.stdin.read()
Path(os.environ["FAKE_DSH_PLAN"] + f".prompt{index}").write_text(prompt)
step = plan[index] if index < len(plan) else {}

if step.get("silent_fail"):
    print("dsh: TIMEOUT: Request timed out.", file=sys.stderr)
    sys.exit(1)


def emit(event):
    print(json.dumps(event), flush=True)


emit({"type": "session", "sessionId": f"session-{index}"})
for name, content in step.get("write", {}).items():
    path = Path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    emit({"type": "tool_call", "tool": "write", "input": {"file_path": name}})
emit({"type": "status", "phase": "step_end", "usage": {"outputTokens": 42}})
if step.get("truncate"):
    emit({"type": "tool_call", "tool": "read", "input": {"file_path": "README.md"}})
    emit({"type": "status", "phase": "turn_end", "reason": {"kind": "max-tokens"}})
    emit({"type": "final", "text": "I need to create the file. Let me read more first."})
    sys.exit(1)
emit({"type": "final", "text": f"fake session {index} done"})
