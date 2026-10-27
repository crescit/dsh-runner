"""Fake `hermes kanban` CLI backed by a JSON file ($FAKE_HERMES_DB), covering the commands dsh-runner uses."""
import argparse
import json
import os
from pathlib import Path
import sys

DB = Path(os.environ["FAKE_HERMES_DB"])


def load():
    return json.loads(DB.read_text()) if DB.exists() else {"tasks": {}, "next": 1}


def save(db):
    DB.write_text(json.dumps(db, indent=2))


def main(argv):
    if argv[:1] != ["kanban"]:
        sys.exit("fake hermes: only `kanban` is supported")
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", required=True)
    parser.add_argument("command")
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv[1:])
    db = load()
    tasks = db["tasks"]
    rest = args.rest

    def flag(name, default=None):
        return rest[rest.index(name) + 1] if name in rest else default

    if args.command == "list":
        print(json.dumps([{k: t[k] for k in ("id", "title", "status", "assignee")} for t in tasks.values()]))
    elif args.command == "create":
        key = flag("--idempotency-key")
        if any(t.get("key") == key for t in tasks.values()):
            return
        tid = f"t_{db['next']:04d}"
        db["next"] += 1
        tasks[tid] = {"id": tid, "title": rest[0], "body": flag("--body", ""), "assignee": flag("--assignee"),
                      "status": "ready", "parents": [], "comments": [], "key": key}
    elif args.command == "show":
        task = tasks[rest[0]]
        print(json.dumps({"task": task, "parents": task["parents"]}))
    elif args.command == "edit":
        tasks[rest[0]].update(title=flag("--title"), body=flag("--body"))
    elif args.command in ("link", "unlink"):
        parent, child = rest[0], rest[1]
        parents = tasks[child]["parents"]
        if args.command == "link" and parent not in parents:
            parents.append(parent)
            if tasks[parent]["status"] != "done":
                tasks[child]["status"] = "todo"
        elif args.command == "unlink" and parent in parents:
            parents.remove(parent)
    elif args.command == "promote":
        tasks[rest[0]]["status"] = "ready"
    elif args.command == "claim":
        tasks[rest[0]]["status"] = "running"
    elif args.command == "comment":
        tasks[rest[0]]["comments"].append(rest[1])
    elif args.command == "complete":
        tasks[rest[0]].update(status="done", result=flag("--result"))
    elif args.command == "block":
        tasks[rest[0]]["status"] = "blocked"
    elif args.command == "reclaim":
        tasks[rest[0]]["status"] = "ready"
    else:
        sys.exit(f"fake hermes: unsupported command {args.command}")
    save(db)


if __name__ == "__main__":
    main(sys.argv[1:])
