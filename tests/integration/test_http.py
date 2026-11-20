import asyncio

import httpx

from team_memory.web import create_app


def test_http_and_mcp_workflow(lab):
    asyncio.run(workflow(lab))


async def workflow(lab):
    app = create_app(lab.s, scheduled=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as api,
    ):

        def auth(who):
            return {"Authorization": "Bearer " + lab.credentials[who]}

        assert (await api.get("/health")).json()["status"] == "ok"
        assert (
            await api.get("/v1/reviews", params={"space": "project:atlas"})
        ).status_code == 401
        url = "/v1/memories"
        data = {"space": "project:atlas", "claim": "test", "request_id": "a"}
        res = (await api.post(url, json=data, headers=auth("ana"))).json()
        durl = "/v1/reviews/" + res["review_id"] + "/decisions"
        decision = {"action": "confirm", "expected_version": 1}
        assert (
            await api.post(durl, json=decision, headers=auth("ana"))
        ).status_code == 403
        assert (
            await api.post(durl, json=decision, headers=auth("person"))
        ).status_code == 200
        assert (
            await api.get(url + "/" + res["memory_id"], headers=auth("eve"))
        ).status_code == 404
        assert "记忆复验" in (await api.get("/review")).text
        data = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_context", "arguments": {"space": "project:atlas"}},
        }
        assert (await api.post("/mcp", json=data)).status_code == 401
        headers = {**auth("lin"), "Accept": "application/json, text/event-stream"}
        reply = await api.post("/mcp", json=data, headers=headers)
        assert reply.status_code == 200, reply.text
        result = reply.json()["result"]
        assert not result.get("isError") and "test" in str(result)
        data["params"]["arguments"] = {"space": "personal:ana"}
        assert (await api.post("/mcp", json=data, headers=headers)).json()["result"][
            "isError"
        ]
        lab.st.revoke_client("lin", "lin")
        assert (await api.post("/mcp", json=data, headers=headers)).status_code == 401
