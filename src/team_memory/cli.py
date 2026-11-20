import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path
from threading import Event

import uvicorn
from pydantic import TypeAdapter

from .adapters import import_manifest, import_workspace
from .domain import MemoryService
from .jobs import Worker
from .models import Failure, Space
from .storage import Store, atomic_file, digest
from .web import create_app


def snapshot(source, dest):
    root = (
        source.root
        if isinstance(source, Store)
        else Path(source).expanduser().resolve(strict=True)
    )
    dest = Path(dest).expanduser().resolve()
    if dest.exists() or dest.is_relative_to(root):
        raise ValueError(
            "destination must be a new directory outside the data directory"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=dest.parent))
    try:
        # SQLite backup fixes a consistent view. Its referenced blobs are immutable.
        with (
            sqlite3.connect(
                (root / "state.sqlite").as_uri() + "?mode=ro", uri=True
            ) as src,
            sqlite3.connect(stage / "state.sqlite") as out,
        ):
            src.backup(out)
            if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("invalid backup")
            (stage / "events").mkdir()
            for (key,) in out.execute("SELECT DISTINCT blob FROM sources"):
                if not re.fullmatch("[0-9a-f]{64}", key):
                    raise ValueError("invalid blob reference")
                name = key + ".jsonl"
                payload = (root / "events" / name).read_bytes()
                if digest(json.loads(payload)["text"]) != key:
                    raise ValueError("source checksum mismatch")
                (stage / "events" / name).write_bytes(payload)
        stage.rename(dest)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def args_parser():
    parser = argparse.ArgumentParser(prog="team-memory")
    parser.add_argument("--root", help="private data directory, also TEAM_MEMORY_DIR")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").add_argument("--data-dir")
    grant = sub.add_parser("grant")
    grant.add_argument("actor")
    grant.add_argument("space")
    grant.add_argument("role", choices=["read", "write", "review"])
    issue = sub.add_parser("token")
    issue.add_argument("actor")
    issue.add_argument("--client", required=True)
    issue.add_argument("--human", action="store_true")
    issue.add_argument(
        "--space", action="append", type=TypeAdapter(Space).validate_python
    )
    issue.add_argument(
        "--max-role", choices=["read", "write", "review"], default="review"
    )
    revoke = sub.add_parser("revoke-token")
    revoke.add_argument("actor")
    revoke.add_argument("--client", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("--manifest", required=True)
    imp.add_argument("--dry-run", action="store_true")
    workspace = sub.add_parser("import-workspace")
    workspace.set_defaults(cmd="import-workspace")
    workspace.add_argument("directory")
    workspace.add_argument("--space", action="append", required=True)
    workspace.add_argument("--dry-run", action="store_true")
    for name in ("bind", "unbind"):
        b = sub.add_parser(name)
        b.add_argument("group")
        b.add_argument("project")
    sub.add_parser("withdraw").add_argument("source_id")
    run = sub.add_parser("digest")
    run.add_argument("--space")
    run.add_argument("--date", type=date.fromisoformat)
    sub.add_parser("worker")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--no-worker", action="store_true")
    sub.add_parser("backup").add_argument("destination")
    restore = sub.add_parser("restore")
    restore.add_argument("snapshot")
    restore.add_argument("destination")
    # Keep the previous command usable by existing scripts, outside the public help.
    sub.metavar = "{" + ",".join(sub.choices) + "}"
    sub.choices["import-copaw"] = workspace
    return parser


def main():
    args = args_parser().parse_args()
    config = (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "team-memory"
        / "location"
    )
    root = (
        getattr(args, "data_dir", None)
        or args.root
        or os.environ.get("TEAM_MEMORY_DIR")
        or (
            config.read_text().strip()
            if config.is_file()
            else "~/.local/share/team-memory"
        )
    )
    if args.cmd == "restore":
        if not (Path(args.snapshot) / "state.sqlite").is_file():
            raise SystemExit("invalid snapshot")
        snapshot(args.snapshot, args.destination)
        return
    store = Store(root)
    service = MemoryService(
        store, os.environ.get("TEAM_MEMORY_TIMEZONE", "Asia/Shanghai")
    )
    schedule = os.environ.get("TEAM_MEMORY_SCHEDULE", "02:00")
    try:
        match args.cmd:
            case "init":
                if args.data_dir:
                    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    atomic_file(config, str(store.root).encode())
                print(store.root)
            case "grant":
                store.grant(
                    args.actor,
                    TypeAdapter(Space).validate_python(args.space),
                    args.role,
                )
            case "token":
                print(
                    store.issue(
                        args.actor,
                        args.client,
                        args.human,
                        spaces=args.space,
                        max_role=args.max_role,
                    )
                )
            case "revoke-token":
                print(store.revoke_client(args.actor, args.client))
            case "import" | "import-workspace":
                if args.cmd == "import":
                    result = import_manifest(service, args.manifest, args.dry_run)
                else:
                    spaces = TypeAdapter(list[Space]).validate_python(args.space)
                    result = import_workspace(
                        service, args.directory, spaces, args.dry_run
                    )
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if any(r["status"] == "failed" for r in result):
                    raise SystemExit(1)
            case "bind" | "unbind":
                service.bind(args.group, args.project, args.cmd == "unbind")
            case "withdraw":
                service.withdraw(args.source_id)
            case "digest":
                result = Worker(service, schedule=schedule).run(args.space, args.date)
                print(json.dumps(result))
                if result["errors"]:
                    raise SystemExit(1)
            case "worker":
                Worker(service, schedule=schedule).loop(Event())
            case "serve":
                uvicorn.run(
                    create_app(
                        service, scheduled=not args.no_worker, schedule=schedule
                    ),
                    host=args.host,
                    port=args.port,
                )
            case "backup":
                snapshot(store, args.destination)
    except (Failure, ValueError, OSError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
