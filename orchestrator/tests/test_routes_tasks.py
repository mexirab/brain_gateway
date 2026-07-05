"""Tests for the task-backlog REST routes (/api/tasks)."""

import pytest


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.routes_tasks import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_add_list_complete_flow(client, tmp_db):
    # empty list
    assert client.get("/api/tasks").json() == []

    # add
    r = client.post("/api/tasks", json={"text": "call the dentist", "priority": "high"})
    assert r.status_code == 201
    task = r.json()
    assert task["text"] == "call the dentist"
    assert task["priority"] == "high"
    assert task["status"] == "open"
    tid = task["id"]

    # list shows it
    tasks = client.get("/api/tasks").json()
    assert len(tasks) == 1 and tasks[0]["id"] == tid

    # complete
    assert client.post(f"/api/tasks/{tid}/complete").json()["ok"] is True
    assert client.get("/api/tasks").json() == []  # gone from open
    assert len(client.get("/api/tasks?status=done").json()) == 1


def test_add_requires_text(client, tmp_db):
    assert client.post("/api/tasks", json={"text": "   "}).status_code == 400


def test_priority_ordering_high_first(client, tmp_db):
    client.post("/api/tasks", json={"text": "normal one"})
    client.post("/api/tasks", json={"text": "urgent one", "priority": "high"})
    order = [t["text"] for t in client.get("/api/tasks").json()]
    assert order[0] == "urgent one"


def test_drop_removes_from_open(client, tmp_db):
    tid = client.post("/api/tasks", json={"text": "reorganize garage"}).json()["id"]
    assert client.post(f"/api/tasks/{tid}/drop").json()["ok"] is True
    assert client.get("/api/tasks").json() == []
    assert client.get("/api/tasks?status=dropped").json()[0]["id"] == tid


def test_patch_priority(client, tmp_db):
    tid = client.post("/api/tasks", json={"text": "x", "priority": "normal"}).json()["id"]
    assert client.patch(f"/api/tasks/{tid}", json={"priority": "high"}).json()["ok"] is True
    assert client.get("/api/tasks").json()[0]["priority"] == "high"
    # invalid priority rejected
    assert client.patch(f"/api/tasks/{tid}", json={"priority": "bogus"}).status_code == 400


def test_complete_unknown_id_is_false(client, tmp_db):
    assert client.post("/api/tasks/nope/complete").json()["ok"] is False
