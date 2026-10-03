"""End-to-end HTTP test for SPEC acceptance scenario 1.

Unlike test_agent_relay.py, nothing here runs in-process: a real uvicorn
server is started as a subprocess, every call goes over a socket, and the
persisted rows are then checked straight from the SQLite file the server
wrote.  The server gets its own throwaway database under pytest's tmp_path,
so the dev server's `./agent-relay.db` is never touched.
"""

from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

PROJECT_DIR = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def live_relay(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("relay") / "relay.db"
    port = free_port()
    env = {
        **os.environ,
        "RELAY_DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "RELAY_LEASE_SECONDS": "60",
    }
    env.pop("DATABASE_URL", None)
    env.pop("RELAY_ENROLLMENT_SECRET", None)
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            if server.poll() is not None:
                pytest.fail(f"relay exited early:\n{server.stdout.read().decode(errors='replace')}")
            try:
                if httpx.get(f"{base_url}/ready", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("relay did not become ready within 30 seconds")
            time.sleep(0.2)
        yield base_url, db_path
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


def test_scenario_1_send_claim_complete_read_over_http(live_relay):
    base_url, db_path = live_relay
    with httpx.Client(base_url=f"{base_url}/api/v1", timeout=40) as client:
        # 1. Register two agents.
        sender_resp = client.post("/agents", json={"name": "sender", "description": "Sends a review task"})
        recipient_resp = client.post("/agents", json={"name": "reviewer", "description": "Reviews Python code"})
        assert sender_resp.status_code == 201
        assert recipient_resp.status_code == 201
        sender, recipient = sender_resp.json(), recipient_resp.json()
        assert set(sender) == {"agent_id", "token"}
        assert sender["agent_id"] != recipient["agent_id"]
        sender_auth = {"Authorization": f"Bearer {sender['token']}"}
        recipient_auth = {"Authorization": f"Bearer {recipient['token']}"}

        # 2. Sender sends a task; the relay records the caller as sender.
        task_input = "Review this Python function: def f(xs): return xs[len(xs)]"
        sent = client.post("/tasks", headers=sender_auth, json={"to": recipient["agent_id"], "input": task_input})
        assert sent.status_code == 201
        task_id = sent.json()["task_id"]
        assert sent.json() == {"task_id": task_id, "status": "queued"}

        queued = client.get(f"/tasks/{task_id}", headers=sender_auth).json()
        assert queued["status"] == "queued"
        assert queued["from"] == sender["agent_id"]
        assert queued["to"] == recipient["agent_id"]
        assert queued["attempt_count"] == 0
        assert queued["output"] is None and queued["error"] is None and queued["finished_at"] is None

        # 3. Recipient claims it.
        claim = client.post("/tasks/claim", headers=recipient_auth, json={"worker_id": "it-worker-1", "wait_seconds": 5})
        assert claim.status_code == 200
        claimed = claim.json()
        assert claimed["task_id"] == task_id
        assert claimed["from"] == sender["agent_id"]
        assert claimed["input"] == task_input
        assert claimed["attempt"] == 1
        assert claimed["claim_token"]
        lease_expires_at = parse_time(claimed["lease_expires_at"])

        processing = client.get(f"/tasks/{task_id}", headers=sender_auth).json()
        assert processing["status"] == "processing"
        assert processing["attempt_count"] == 1

        # Nothing else is waiting in the recipient's inbox.
        assert client.post("/tasks/claim", headers=recipient_auth, json={"wait_seconds": 0}).status_code == 204

        # 5. Recipient completes it.
        output = "Off-by-one: xs[len(xs)] raises IndexError; use xs[-1]."
        complete = client.post(
            f"/tasks/{task_id}/complete",
            headers=recipient_auth,
            json={"claim_token": claimed["claim_token"], "output": output},
        )
        assert complete.status_code == 200
        assert complete.json() == {"task_id": task_id, "status": "completed"}

        # 6. Sender reads the result and delivery history.
        result = client.get(f"/tasks/{task_id}", headers=sender_auth)
        assert result.status_code == 200
        result = result.json()
        assert result["status"] == "completed"
        assert result["output"] == output
        assert result["error"] is None
        assert result["attempt_count"] == 1
        assert parse_time(result["finished_at"]) >= parse_time(result["created_at"])

        attempts = client.get(f"/tasks/{task_id}/attempts", headers=sender_auth)
        assert attempts.status_code == 200
        [attempt] = attempts.json()["items"]
        assert attempt["attempt"] == 1
        assert attempt["worker_id"] == "it-worker-1"
        assert attempt["outcome"] == "completed"
        assert attempt["finished_at"] is not None
        assert parse_time(attempt["lease_expires_at"]) - parse_time(attempt["claimed_at"]) == timedelta(seconds=60)
        assert parse_time(attempt["lease_expires_at"]) == lease_expires_at
        assert "claim_token" not in attempt

        sent_list = client.get("/tasks", headers=sender_auth, params={"direction": "sent"}).json()
        assert [item["task_id"] for item in sent_list["items"]] == [task_id]
        assert sent_list["next_cursor"] is None

    # The outcome is persisted in the real database file, with secrets hashed.
    secrets = [sender["token"], recipient["token"], claimed["claim_token"]]
    db = sqlite3.connect(db_path)
    try:
        status, stored_output, sender_id, recipient_id, attempt_count = db.execute(
            "SELECT status, output, sender_id, recipient_id, attempt_count FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert (status, stored_output, attempt_count) == ("completed", output, 1)
        assert (sender_id, recipient_id) == (sender["agent_id"], recipient["agent_id"])

        outcome, claim_token_hash = db.execute(
            "SELECT outcome, claim_token_hash FROM attempts WHERE task_id = ?", (task_id,)
        ).fetchone()
        assert outcome == "completed"
        assert claim_token_hash == hashlib.sha256(claimed["claim_token"].encode()).hexdigest()

        for agent in (sender, recipient):
            [token_hash] = db.execute("SELECT token_hash FROM agents WHERE id = ?", (agent["agent_id"],)).fetchone()
            assert token_hash == hashlib.sha256(agent["token"].encode()).hexdigest()
    finally:
        db.close()

    for path in db_path.parent.glob(f"{db_path.name}*"):
        raw = path.read_bytes()
        for secret in secrets:
            assert secret.encode() not in raw, f"plaintext secret found in {path.name}"
