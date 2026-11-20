import json
import sqlite3
from datetime import date
from pathlib import Path

from team_memory.adapters import copaw_import, import_manifest
from team_memory.cli import snapshot
from team_memory.domain import MemoryService
from team_memory.jobs import Worker
from team_memory.models import Context
from team_memory.storage import Store


def test_atlas_import_backup_restore(tmp_path):
    st = Store(tmp_path / "lab")
    svc = MemoryService(st)
    for space in ("personal:demo", "project:atlas"):
        st.grant("demo", space, "review")
    manifest = Path(__file__).resolve().parents[2] / "examples/codex/atlas.toml"
    assert [r["status"] for r in import_manifest(svc, manifest, True)] == ["new", "new"]
    assert [r["status"] for r in import_manifest(svc, manifest)] == ["added", "added"]
    assert [r["status"] for r in import_manifest(svc, manifest)] == [
        "existing",
        "existing",
    ]
    assert Worker(svc).run(day=date(2026, 11, 16))["facts"] == 3
    token = st.issue("demo", "client")
    snapshot(st, tmp_path / "backup")
    snapshot(Store(tmp_path / "backup"), tmp_path / "restored")
    restored = MemoryService(Store(tmp_path / "restored"))
    who = restored.store.authenticate(token)
    assert (
        "2026-11-23"
        in restored.context(who, Context(space="project:atlas"))["current"][0]["claim"]
    )
    assert len(restored.reviews(who, "personal:demo")) == 1


def test_feishu_and_copaw_migration(lab, tmp_path):
    root = tmp_path / "legacy"
    raw = root / "group_workspace" / "a" / "raw" / "2026-11-16"
    raw.mkdir(parents=True)
    msg = dict(
        msg_id="m1",
        text="群内讨论",
        sender_id="person",
        timestamp="2026-11-16 10:00:00",
    )
    (raw / "chat.jsonl").write_text(json.dumps(msg) + "\n" + json.dumps(msg))
    doc = raw.parents[1] / "memory.md"
    doc.write_text("群报告")
    old = sqlite3.connect(root / "memory.db")
    old.execute("create table memories(id,source_file,scope,is_deleted)")
    old.executemany(
        "insert into memories values(?,?,?,0)",
        [("old1", "group_workspace/a/memory.md", "a"), ("old2", "gone", "a")],
    )
    old.commit()
    old.close()
    report = copaw_import(lab.s, root, ["group:a"])
    assert sorted(i["status"] for i in report) == [
        "added",
        "added",
        "existing",
        "mapped",
        "quarantined",
    ]
    assert all(i["status"] != "added" for i in copaw_import(lab.s, root, ["group:a"]))


def test_feishu_recall_does_not_cross_groups(lab, tmp_path):
    from team_memory.adapters import archive

    path = tmp_path / "day.jsonl"
    msg = {
        "msg_id": "om-test",
        "text": "待复核消息",
        "timestamp": "2026-11-16 12:00:00",
    }
    path.write_text(json.dumps(msg))
    for space in ("group:a", "group:b"):
        archive(lab.s, path, space, "same-date", False)
    Worker(lab.s).run(day=date(2026, 11, 16))
    path.write_text("{invalid json")
    assert archive(lab.s, path, "group:a", "same-date", False)[0]["status"] == "failed"
    assert len(lab.s.reviews(lab.ana, "group:a")) == 1
    path.write_text("")
    assert (
        archive(lab.s, path, "group:a", "same-date", False)[0]["status"] == "withdrawn"
    )
    assert not lab.s.reviews(lab.ana, "group:a")
    assert len(lab.s.reviews(lab.ana, "group:b")) == 1


def test_copaw_hidden_blocks_do_not_enter_memory(lab, tmp_path):
    root = tmp_path / "old"
    doc = root / "project" / "atlas" / "knowledge" / "memory.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "公开记录\n<!-- project_memory_block hash=x state=forgotten -->\n已遗忘内容\n<!-- project_memory_block hash=y state=active source_chat_id=secret -->\n跨群原文"
    )
    imported = copaw_import(lab.s, root, ["project:atlas"])[0]
    with lab.st.tx(False) as db:
        key = db.execute(
            "SELECT blob FROM sources WHERE id=?", (imported["source_id"],)
        ).fetchone()[0]
    assert lab.st.read_blob(key) == "公开记录"
    doc.write_text("<!-- project_memory_block hash=x state=revoked -->\n余下均已撤回")
    assert copaw_import(lab.s, root, ["project:atlas"])[0]["status"] == "quarantined"
    with lab.st.tx(False) as db:
        assert not db.execute("SELECT 1 FROM sources WHERE live=1").fetchone()


def test_cli_quickstart_and_readonly_restore(tmp_path, monkeypatch, capsys):
    import sys

    from team_memory.cli import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("TEAM_MEMORY_DIR", raising=False)

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["team-memory", *map(str, args)])
        main()
        return capsys.readouterr().out

    run("init", "--data-dir", tmp_path / "data")
    for space in ["project:atlas", "personal:demo"]:
        run("grant", "demo", space, "review")
    manifest = Path(__file__).resolve().parents[2] / "examples/codex/atlas.toml"
    assert len(json.loads(run("import", "--manifest", manifest))) == 2
    assert json.loads(run("digest", "--date", "2026-11-16"))["facts"] == 3
    token = run(
        "token",
        "demo",
        "--client",
        "codex",
        "--space",
        "project:atlas",
        "--max-role",
        "read",
    ).strip()
    run("backup", tmp_path / "snapshot")
    before = (tmp_path / "snapshot" / "state.sqlite").read_bytes()
    run("restore", tmp_path / "snapshot", tmp_path / "restored")
    assert (tmp_path / "snapshot" / "state.sqlite").read_bytes() == before
    assert Store(tmp_path / "restored").authenticate(token)["max_role"] == "read"
    assert run("revoke-token", "demo", "--client", "codex").strip() == "1"
