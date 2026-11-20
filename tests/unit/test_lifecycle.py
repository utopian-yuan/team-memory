from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from team_memory.models import Context, Decision, Failure, Query, now


def test_replay_and_conflict(lab):
    r = lab.record("建议下周发布", request_id="one")
    assert lab.record("建议下周发布", request_id="one") == r
    with pytest.raises(Failure, match="already used"):
        lab.record(" changed ", request_id="one")
    assert lab.s.context(lab.ana, Context(space="project:atlas"))["current"] == []
    assert len(lab.s.reviews(lab.ana, "project:atlas")) == 1


def test_simultaneous_review(lab):
    r = lab.record("initial")

    def apply(_):
        try:
            lab.decide(r)
            return 200
        except Failure as e:
            return e.code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(apply, range(2))) == [200, 409]


def test_edit_defer_reject_and_supersede(lab):
    r = lab.record("周五发布", topic="release")
    lab.decide(r, "defer", remind_at=now() + timedelta(hours=2))
    assert not lab.s.reviews(lab.ana, "project:atlas")
    lab.decide(r, "edit", expected_version=2, claim="周一发布")
    old = lab.s.get(lab.lin, r["memory_id"])
    assert old["claim"] == "周一发布"
    new = lab.record("周二发布", topic="release")
    assert new["state"] == "candidate"
    assert lab.s.get(lab.lin, r["memory_id"])["version"] == 1
    lab.decide(new)
    assert lab.s.get(lab.lin, r["memory_id"], 1)["state"] == "superseded"
    assert lab.s.get(lab.lin, r["memory_id"])["claim"] == "周二发布"
    rejected = lab.record("周三发布", topic="release")
    lab.decide(rejected, "reject")
    assert lab.s.get(lab.lin, r["memory_id"])["claim"] == "周二发布"
    assert not lab.s.reviews(lab.ana, "project:atlas")


def test_privacy_all_reads(lab):
    sid = lab.source("私有计划", space="personal:ana")
    r = lab.record("私有计划", sid, space="personal:ana")
    lab.decide(r)
    with pytest.raises(Failure):
        lab.s.get(lab.lin, r["memory_id"])
    with pytest.raises(Failure):
        lab.s.search(lab.lin, Query(space="personal:ana", query="私有"))
    with pytest.raises(Failure):
        lab.s.reviews(lab.lin, "personal:ana")
    assert not lab.s.search(lab.lin, Query(space="project:atlas", query="私有"))
    with pytest.raises(Failure):
        lab.st.grant("eve", "personal:ana", "read")


def test_group_derived_only_and_unlink(lab):
    sid = lab.source("群内原话：内部约定", space="group:a")
    lab.s.bind("group:a", "project:atlas")
    with pytest.raises(Failure):
        lab.record("可公开的决定", sid, quote="内部约定")
    r = lab.record("可公开的决定", sid, quote="内部约定", who=lab.person)
    lab.decide(r)
    assert lab.s.get(lab.lin, r["memory_id"])["evidence"] == []
    assert not lab.s.search(lab.lin, Query(space="project:atlas", query="群"))
    lab.s.bind("group:a", "project:atlas", remove=True)
    assert not lab.s.search(lab.lin, Query(space="project:atlas", query="决定"))
    with pytest.raises(Failure):
        lab.s.get(lab.lin, r["memory_id"], 1)


def test_scope_checked_on_replay(lab):
    lab.record("可写", request_id="r")
    lab.st.grant("ana", "project:atlas", "read")
    with pytest.raises(Failure):
        lab.record("可写", request_id="r")


def test_token_scope_restricts_evidence_and_writes(lab):
    sid = lab.source("项目组借用群内结论", space="group:a")
    lab.s.bind("group:a", "project:atlas")
    r = lab.record("已授权的项目说明", sid, quote="群内结论", who=lab.person)
    lab.decide(r)
    token = lab.st.issue("ana", "reader", spaces=["project:atlas"], max_role="read")
    scoped = lab.st.authenticate(token)
    assert lab.s.get(scoped, r["memory_id"])["evidence"] == []
    with pytest.raises(Failure):
        lab.record("write", who=scoped)
    with pytest.raises(Failure):
        lab.s.context(scoped, Context(space="group:a"))


def test_human_evidence_and_revoke(lab):
    r = lab.record("周日演练")
    lab.decide(r)
    row = lab.s.get(lab.ana, r["memory_id"])
    assert row["evidence"][0]["adapter"] == "human_review"
    pending = lab.record("改到周一", memory_id=r["memory_id"], base_version=1)
    lab.s.revoke_memory(lab.person, r["memory_id"], 1)
    with pytest.raises(Failure):
        lab.s.get(lab.ana, r["memory_id"], 1)
    assert not lab.s.reviews(lab.person, "project:atlas")
    with pytest.raises(Failure):
        lab.decide(pending)


def test_historical_bitemporal_query(lab):
    from datetime import datetime, timezone

    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    r = lab.record("November decision", topic="d", effective_at=start)
    lab.decide(r)
    recorded = now()
    second = lab.record(
        "December decision", topic="d", effective_at=start + timedelta(days=30)
    )
    lab.decide(second)
    assert (
        lab.s.get(lab.ana, r["memory_id"], as_of=start + timedelta(days=1))["claim"]
        == "November decision"
    )
    assert (
        lab.s.get(
            lab.ana, r["memory_id"], as_of=start + timedelta(days=32), known_at=recorded
        )["claim"]
        == "November decision"
    )
    assert (
        lab.s.get(lab.ana, r["memory_id"], as_of=start + timedelta(days=32))["claim"]
        == "December decision"
    )


def test_recheck_and_revision_preserve_prior_claim(lab):
    from team_memory.jobs import Worker

    # A new approval clears an already-due reminder. Set a future reminder and advance the worker's clock.
    future = now() + timedelta(days=2)
    r = lab.record("之后复查", recheck_at=future)
    lab.decide(r)
    Worker(lab.s).run(clock=future + timedelta(seconds=1))
    assert not lab.s.context(lab.ana, Context(space="project:atlas"))["current"]
    review = [
        item
        for item in lab.s.reviews(lab.ana, "project:atlas")
        if item["memory"] == r["memory_id"]
    ][0]
    result = lab.s.decide(
        lab.person,
        review["id"],
        Decision(action="edit", claim="已经修正", expected_version=1),
    )
    assert result["version"] == 2
    assert lab.s.get(lab.ana, r["memory_id"], 1)["claim"] == "之后复查"
    assert lab.s.get(lab.ana, r["memory_id"])["claim"] == "已经修正"
