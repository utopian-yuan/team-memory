import json
import re
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from .domain import retire_source
from .models import WORKSPACE_STORAGE_KEY, Failure, Source
from .storage import packed


def instant(value, zone):
    if isinstance(value, (int, float)) or str(value).isdigit():
        n = float(value)
        return datetime.fromtimestamp(n / 1000 if n > 10**11 else n, timezone.utc)
    dt = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    return dt if dt.tzinfo else dt.replace(tzinfo=zone)


def archive(service, path, space, archive_id, dry_run):
    """A completely parsed snapshot can reconcile recalls from the same archive."""
    text = path.read_text()
    collected, report = set(), []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if raw.get("chat_id") and space != "group:" + raw["chat_id"]:
                raise ValueError("archive group mismatch")
            src = Source(
                space=space,
                adapter="feishu_group",
                external_id=raw["msg_id"],
                text=raw["text"],
                author=raw.get("sender_id"),
                occurred_at=instant(raw["timestamp"], service.zone),
                revision=raw.get("revision"),
            )
            report.append(service.ingest(src, dry_run=dry_run))
            collected.add(src.external_id)
        except (ValueError, KeyError, TypeError, Failure) as exc:
            report.append(
                {"status": "failed", "line": line_no, "error": type(exc).__name__}
            )
    if any(item["status"] == "failed" for item in report):
        return report
    with service.store.tx(not dry_run) as db:
        if path.read_text() != text:
            raise ValueError("archive changed during import; retry")
        prev = {
            r[0]
            for r in db.execute(
                "SELECT external_id FROM archive_members WHERE space=? AND archive=?",
                (space, archive_id),
            )
        }
        if not dry_run:
            db.executemany(
                "INSERT OR IGNORE INTO archive_members VALUES (?,?,?)",
                [(space, archive_id, i) for i in collected - prev],
            )
        for missing in sorted(prev - collected):
            other = db.execute(
                "SELECT 1 FROM archive_members WHERE space=? AND external_id=? AND archive!=?",
                (space, missing, archive_id),
            ).fetchone()
            if not dry_run:
                db.execute(
                    "DELETE FROM archive_members WHERE space=? AND archive=? AND external_id=?",
                    (space, archive_id, missing),
                )
            if not other:
                row = db.execute(
                    "SELECT id FROM sources WHERE space=? AND adapter='feishu_group' AND external_id=? AND live=1",
                    (space, missing),
                ).fetchone()
                if row:
                    if not dry_run:
                        retire_source(db, row["id"])
                    report.append({"status": "withdrawn", "source_id": row["id"]})
    return report


def import_manifest(service, manifest, dry_run=False):
    manifest = Path(manifest)
    specs = tomllib.loads(manifest.read_text())["sources"]
    report = []
    for spec in specs:
        try:
            path = (manifest.parent / spec["path"]).resolve(strict=True)
            adapter, space = spec["adapter"], spec["space"]
            if adapter == "feishu_group":
                report.extend(
                    archive(service, path, space, spec["external_id"], dry_run)
                )
            else:
                timestamp = spec.get("occurred_at", path.stat().st_mtime)
                src = Source(
                    space=space,
                    adapter=adapter,
                    external_id=spec["external_id"],
                    text=path.read_text(),
                    author=spec.get("author"),
                    occurred_at=instant(timestamp, service.zone),
                    revision=spec.get("revision"),
                )
                report.append(
                    service.ingest(
                        src, trusted=spec.get("verified") is True, dry_run=dry_run
                    )
                )
        except (ValueError, TypeError, KeyError, OSError, Failure) as exc:
            report.append(
                {
                    "status": "failed",
                    "entry": spec.get("external_id", spec.get("adapter")),
                    "error": str(exc),
                }
            )
    return report


def visible_legacy_text(text, project):
    # Marked material from another group needs a current binding and explicit publication.
    visible = True
    lines = []
    for line in text.splitlines():
        if "<!-- project_memory_block" in line:
            attrs = dict(re.findall(r"(\w+)=([\w:.-]+)", line))
            visible = (
                bool(attrs.get("hash")) and attrs.get("state", "active") == "active"
            )
            if project and attrs.get("source_chat_id"):
                visible = False
        elif visible:
            lines.append(line)
    return "\n".join(lines).strip()


def import_workspace(service, root, spaces, dry_run=False):
    """Import allowlisted workspaces; preserve selected legacy rows in an operator-only ledger."""
    root = Path(root).resolve(strict=True)
    report, files = [], {}
    mapping = {
        "project": "project",
        "personal": "personal_workspace",
        "group": "group_workspace",
    }
    for space in spaces:
        kind, label = space.split(":", 1)
        directory = (root / mapping[kind] / label).resolve()
        if not directory.is_relative_to(root / mapping[kind]):
            raise ValueError("invalid workspace")
        if not directory.is_dir():
            report.append(
                {"status": "failed", "space": space, "error": "workspace missing"}
            )
            continue
        patterns = {
            "project": ["knowledge/*.md", "digest/**/*.md"],
            "group": ["memory.md", "digest/**/*.md", "raw/*/chat.jsonl"],
            "personal": ["memory.md", "digest/**/*.md"],
        }[kind]
        for pattern in patterns:
            for file in sorted(directory.glob(pattern)):
                if (
                    not file.is_file()
                    or file.is_symlink()
                    or not file.resolve().is_relative_to(directory)
                ):
                    continue
                relative = str(file.relative_to(root))
                try:
                    if file.name == "chat.jsonl":
                        report.extend(archive(service, file, space, relative, dry_run))
                        continue
                    content = visible_legacy_text(file.read_text(), kind == "project")
                    if not content:
                        with service.store.tx(not dry_run) as db:
                            old = db.execute(
                                "SELECT id FROM sources WHERE space=? AND adapter=? AND external_id=? AND live=1",
                                (space, WORKSPACE_STORAGE_KEY, relative),
                            ).fetchone()
                            if old and not dry_run:
                                retire_source(db, old["id"])
                        report.append(
                            {
                                "status": "quarantined",
                                "path": relative,
                                "reason": "no_visible_content",
                            }
                        )
                        continue
                    src = Source(
                        space=space,
                        adapter="assistant_workspace",
                        external_id=relative,
                        text=content,
                        occurred_at=instant(file.stat().st_mtime, service.zone),
                    )
                    result = service.ingest(src, dry_run=dry_run)
                    files[relative] = (space, result.get("source_id"))
                    report.append({**result, "path": relative})
                except (ValueError, KeyError, OSError, Failure) as exc:
                    report.append(
                        {"status": "failed", "path": relative, "error": str(exc)}
                    )
    db_path = root / "memory.db"
    if db_path.is_file():
        old = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
        old.row_factory = sqlite3.Row
        try:
            with service.store.tx(not dry_run) as db:
                for row in old.execute("SELECT * FROM memories WHERE is_deleted=0"):
                    resolved = (root / (row["source_file"] or "")).resolve()
                    rel = (
                        str(resolved.relative_to(root))
                        if resolved.is_relative_to(root)
                        else None
                    )
                    matches = [
                        s for s in spaces if row["scope"] in (s, s.split(":", 1)[1])
                    ]
                    space, source = files.get(rel, (None, None))
                    if not space and len(matches) != 1:
                        continue
                    scoped = space or matches[0]
                    mapped = space is not None and (not matches or space in matches)
                    status = "mapped" if mapped else "quarantined"
                    if not dry_run:
                        db.execute(
                            "INSERT OR REPLACE INTO legacy_records VALUES (?,?,?,?,?)",
                            (
                                scoped,
                                row["id"],
                                source if mapped else None,
                                status,
                                packed(dict(row)),
                            ),
                        )
                    report.append(
                        {
                            "status": status,
                            "legacy_id": row["id"],
                            "source_id": source if mapped else None,
                        }
                    )
        finally:
            old.close()
    return report


# Compatibility with programmatic callers of the original importer.
copaw_import = import_workspace
