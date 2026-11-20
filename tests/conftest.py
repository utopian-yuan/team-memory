from datetime import datetime, timezone

import pytest

from team_memory.domain import MemoryService
from team_memory.models import Decision, Evidence, Record, Source
from team_memory.storage import Store


@pytest.fixture
def lab(tmp_path):
    class Lab:
        def __init__(self):
            self.st = Store(tmp_path / "data")
            self.s = MemoryService(self.st)
            for user, space, role in [
                ("ana", "personal:ana", "review"),
                ("ana", "project:atlas", "review"),
                ("ana", "group:a", "review"),
                ("ana", "group:b", "review"),
                ("lin", "project:atlas", "read"),
                ("eve", "project:else", "review"),
            ]:
                self.st.grant(user, space, role)
            self.credentials = {}
            for name, user, human in [
                ("ana", "ana", False),
                ("person", "ana", True),
                ("lin", "lin", False),
                ("eve", "eve", False),
            ]:
                token = self.st.issue(user, name, human)
                self.credentials[name] = token
                setattr(self, name, self.st.authenticate(token))

        def source(
            self, text="项目确认上线", space="project:atlas", key="meeting", **kw
        ):
            defaults = dict(
                author="ana",
                adapter="project_document",
                occurred_at=datetime(2026, 11, 16, tzinfo=timezone.utc),
            )
            src = Source(space=space, external_id=key, text=text, **(defaults | kw))
            return self.s.ingest(src, trusted=True)["source_id"]

        def record(
            self, claim, source=None, quote=None, space="project:atlas", who=None, **kw
        ):
            refs = [Evidence(source_id=source, quote=quote or claim)] if source else []
            record = Record(
                space=space,
                claim=claim,
                evidence=refs,
                request_id=kw.pop("request_id", claim),
                **kw,
            )
            return self.s.record(who or self.ana, record)

        def decide(self, r, action="confirm", expected_version=1, **kw):
            return self.s.decide(
                self.person,
                r["review_id"],
                Decision(action=action, expected_version=expected_version, **kw),
            )

    return Lab()
