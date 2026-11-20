from datetime import date, datetime, timezone

import pytest

from team_memory.jobs import Fact, Worker, daily
from team_memory.models import Context, Failure, Query


def test_daily_recovery_and_evidence(lab):
    source = lab.source("[decision|release] 负责人确认下周一上线。\n猜测")
    day = date(2026, 11, 16)
    before = datetime(2026, 11, 16, 17, 59, tzinfo=timezone.utc)
    after = datetime(2026, 11, 16, 18, tzinfo=timezone.utc)
    worker = Worker(lab.s)
    assert worker.run(clock=before)["sources"] == 0

    def broken(text):
        return [Fact(claim="未知", quote="缺失引文")]

    assert Worker(lab.s, broken).run(clock=after)["errors"]
    assert not lab.s.reviews(lab.ana, "project:atlas")
    assert worker.run(clock=after)["facts"] == 2
    assert worker.run(clock=after)["facts"] == 0
    item = lab.s.search(lab.lin, Query(space="project:atlas", query="上线"))[0]
    assert "2026-11-23" in item["claim"]
    assert item["evidence"][0]["source_id"] == source
    assert len(lab.s.reviews(lab.ana, "project:atlas")) == 1
    lab.source("[progress|later] 新资料", key="late")
    assert worker.run(clock=after)["facts"] == 1
    assert "负责人确认" in daily(lab.s, lab.lin, "project:atlas", day)
    lab.s.withdraw(source)
    assert "负责人确认" not in daily(lab.s, lab.lin, "project:atlas", day)
    assert not lab.s.search(lab.lin, Query(space="project:atlas", query="上线"))
    assert not lab.s.reviews(lab.ana, "project:atlas")


def test_revised_sources_reject_stale(lab):
    lab.source("[preference|p] 旧偏好", revision="1")
    Worker(lab.s).run(day=date(2026, 11, 16))
    with pytest.raises(Failure, match="different"):
        lab.source("其他", revision="1")
    lab.source("[preference|p] 新偏好", revision="2")
    assert not lab.s.context(lab.lin, Context(space="project:atlas"))["current"]
    Worker(lab.s).run(day=date(2026, 11, 16))
    assert len(lab.s.context(lab.lin, Context(space="project:atlas"))["current"]) == 1


def test_external_claims_cannot_self_verify(lab):
    lab.source("[decision|d] 已确认上线", adapter="codex_summary")
    Worker(lab.s).run(day=date(2026, 11, 16))
    assert (
        lab.s.reviews(lab.ana, "project:atlas")[0]["candidate"]["state"] == "candidate"
    )


def test_failed_batch_is_atomic(lab):
    lab.source("证据甲")

    def partial(text):
        return [Fact(claim=text, quote=text), Fact(claim="第二条", quote="不存在")]

    assert Worker(lab.s, partial).run(day=date(2026, 11, 16))["errors"]
    assert not lab.s.reviews(lab.ana, "project:atlas")


def test_model_payload_retries_and_evidence(lab, monkeypatch):
    import httpx

    monkeypatch.setenv("TEAM_MEMORY_MODEL_URL", "http://model.test/chat/completions")
    monkeypatch.setenv("TEAM_MEMORY_MODEL", "fixture")

    def fake(url, **params):
        assert params["json"]["messages"][-1]["content"] == "用户原话"
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"facts":[{"claim":"可能结论","quote":"用户原话","confidence":0.42}]}'
                        }
                    }
                ]
            },
            request=request,
        )

    monkeypatch.setattr(httpx, "post", fake)
    lab.source("用户原话")
    assert not Worker(lab.s).run(day=date(2026, 11, 16))["errors"]
    reason = lab.s.reviews(lab.ana, "project:atlas")[0]["candidate"]["reason"]
    assert "low_confidence" in reason and "derived_claim" in reason


def test_two_independent_sources_survive_one_withdrawal(lab):
    first = lab.source("[decision|window] 决定本次只做内部试验", key="first")
    second = lab.source("[decision|window] 决定本次只做内部试验", key="second")
    Worker(lab.s).run(day=date(2026, 11, 16))
    q = Context(space="project:atlas")
    assert set(lab.s.context(lab.lin, q)["current"][0]["references"]) == {first, second}
    lab.s.withdraw(first)
    assert lab.s.context(lab.lin, q)["current"][0]["references"] == [second]
    lab.s.withdraw(second)
    assert not lab.s.context(lab.lin, q)["current"]


def test_agent_source_cannot_overwrite_operator_source(lab):
    from datetime import datetime, timezone

    from team_memory.models import Source

    sid = lab.source("原始来源", key="same")
    src = Source(
        space="project:atlas",
        adapter="project_document",
        external_id="same",
        text="agent copy",
        author="ana",
        occurred_at=datetime.now(timezone.utc),
    )
    remote = lab.s.ingest(src, lab.ana)
    with lab.st.tx(False) as db:
        assert db.execute("SELECT live FROM sources WHERE id=?", (sid,)).fetchone()[0]
        assert not db.execute(
            "SELECT trusted FROM sources WHERE id=?", (remote["source_id"],)
        ).fetchone()[0]


def test_partial_evidence_in_negated_sentence_requires_review(lab):
    lab.source("未确认上线。")

    def extracted(_):
        return [
            Fact(
                kind="decision",
                topic="release",
                confidence=1,
                claim="确认上线",
                quote="确认上线",
            )
        ]

    assert not Worker(lab.s, extracted).run(day=date(2026, 11, 16))["errors"]
    assert not lab.s.context(lab.ana, Context(space="project:atlas"))["current"]
    assert (
        "partial_context"
        in lab.s.reviews(lab.ana, "project:atlas")[0]["candidate"]["reason"]
    )
