"""service.py 端到端（TestClient）：4 端点 + SSE 骨架。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from matrag_agent.service import app

client = TestClient(app)

_EXPECTED_ROUTES = {
    "/agent/dev",
    "/agent/debug",
    "/agent/review",
    "/agent/{job_id}/events",
}


def test_routes_registered() -> None:
    """4 个端点（3 POST + 1 SSE）均已注册。"""
    paths = {r.path for r in app.routes}
    assert _EXPECTED_ROUTES <= paths


def test_dev_returns_stub_job() -> None:
    resp = client.post("/agent/dev", json={"product_code": "PA601-D54A", "text": "输出电压 54V"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "stub"
    assert body["job_id"]


def test_debug_returns_stub_job() -> None:
    resp = client.post("/agent/debug", json={"execution_id": "EX-1"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "stub"


def test_review_returns_stub_job() -> None:
    resp = client.post("/agent/review", json={"bundle": {"product_code": "X"}})
    assert resp.status_code == 200
    assert resp.json()["status"] == "stub"


def test_endpoints_accept_empty_body() -> None:
    """骨架端点在空 body 下也应返回 stub（宽松请求体）。"""
    for path in ("/agent/dev", "/agent/debug", "/agent/review"):
        resp = client.post(path)
        assert resp.status_code == 200, path
        assert resp.json()["status"] == "stub"


def test_job_ids_unique() -> None:
    a = client.post("/agent/dev", json={}).json()["job_id"]
    b = client.post("/agent/dev", json={}).json()["job_id"]
    assert a != b


def test_events_stream_known_job() -> None:
    """已知 job 的 SSE 流返回事件（text/event-stream）。"""
    job_id = client.post("/agent/review", json={}).json()["job_id"]
    with client.stream("GET", f"/agent/{job_id}/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())
    assert ": connected" in body
    assert "event: status" in body
    assert job_id in body


def test_events_unknown_job_404() -> None:
    resp = client.get("/agent/does-not-exist/events")
    assert resp.status_code == 404
