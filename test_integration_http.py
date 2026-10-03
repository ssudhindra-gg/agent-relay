"""End-to-end HTTP test for SPEC acceptance scenario 1.

Unlike test_agent_relay.py, nothing here runs in-process.  The same scenario
runs against two real deployments of the relay:

- ``process``: a uvicorn subprocess on a free port, using a fresh database on
  the session's throwaway PostgreSQL server (see conftest.py).
- ``compose``: the full docker-compose.yaml stack (relay image rebuilt from
  the working tree, plus its ``postgres`` service) under a unique project name
  with random localhost ports and its own volume.  Skipped when Docker is
  unavailable.
- ``kubernetes``: the k8s/ manifests applied to a local kind cluster
  (``RELAY_TEST_KIND_CLUSTER``, default ``kind``) in a throwaway namespace,
  with the image built under a unique tag and loaded into the cluster, reached
  through ``kubectl port-forward``.  Skipped when kind, kubectl, or that
  cluster is unavailable.  Every kubectl call names the kind context, so no
  other cluster is ever touched.

Every call goes over a socket, and the persisted rows are then checked
directly in PostgreSQL.  Each deployment gets a throwaway database, so neither
a developer database, a long-running ``docker compose`` stack, nor the
cluster's ``agent-relay`` namespace is touched.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest

from conftest import PUBLISHED_HOST, create_database, drop_database

PROJECT_DIR = Path(__file__).resolve().parent
# In CI every target must run: a missing tool or cluster is an error, not a skip.
REQUIRE_TARGETS = os.getenv("RELAY_TEST_REQUIRE_TARGETS", "").lower() in {"1", "true", "yes"}


@dataclass
class LiveRelay:
    base_url: str
    # libpq URL of the relay's database, reachable from this machine.
    database_url: str


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def wait_until_ready(base_url: str, exit_log: Callable[[], str | None]) -> None:
    """Poll /ready; fail fast with the server's output if it dies first."""
    deadline = time.monotonic() + 60
    while True:
        log = exit_log()
        if log is not None:
            pytest.fail(f"relay exited early:\n{log}")
        try:
            if httpx.get(f"{base_url}/ready", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            pytest.fail("relay did not become ready within 60 seconds")
        time.sleep(0.2)


def target_unavailable(reason: str):
    if REQUIRE_TARGETS:
        pytest.fail(f"{reason} (RELAY_TEST_REQUIRE_TARGETS is set)")
    pytest.skip(reason)


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


@pytest.fixture(scope="module")
def process_relay(postgres_admin_url):
    database_url = create_database(postgres_admin_url, "relay_it")
    port = free_port()
    env = {**os.environ, "RELAY_DATABASE_URL": database_url, "RELAY_LEASE_SECONDS": "60"}
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
        wait_until_ready(
            base_url,
            lambda: server.stdout.read().decode(errors="replace") if server.poll() is not None else None,
        )
        yield LiveRelay(base_url, database_url.replace("postgresql+psycopg://", "postgresql://", 1))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        drop_database(postgres_admin_url, database_url)


@pytest.fixture(scope="module")
def compose_relay():
    if not docker_available():
        target_unavailable("Docker is not available")
    project = f"relay-it-{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex
    env = {
        **os.environ,
        "POSTGRES_PASSWORD": password,
        # Host port 0 = Docker picks a free one, so a running stack is untouched.
        "RELAY_PORT": "127.0.0.1:0",
        "POSTGRES_PORT": "127.0.0.1:0",
        "RELAY_LEASE_SECONDS": "60",
        "RELAY_MAX_ATTEMPTS": "5",
        "RELAY_ENROLLMENT_SECRET": "",
    }

    def compose(*args: str, timeout: float = 120) -> str:
        result = subprocess.run(
            ["docker", "compose", "-p", project, *args],
            cwd=PROJECT_DIR, env=env, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            logs = subprocess.run(
                ["docker", "compose", "-p", project, "logs", "--no-color"],
                cwd=PROJECT_DIR, env=env, capture_output=True, text=True, timeout=60,
            ).stdout
            pytest.fail(f"docker compose {' '.join(args)} failed:\n{result.stderr}\n{logs}")
        return result.stdout.strip()

    def host_port(service: str, port: int) -> str:
        return compose("port", service, str(port)).splitlines()[0].rsplit(":", 1)[1]

    try:
        # --build so the stack runs the code under test, not a stale image.
        compose("up", "-d", "--build", "--wait", "--wait-timeout", "120", timeout=900)
        base_url = f"http://{PUBLISHED_HOST}:{host_port('relay', 8000)}"
        database_url = f"postgresql://relay:{password}@{PUBLISHED_HOST}:{host_port('postgres', 5432)}/relay"

        def exit_log() -> str | None:
            if compose("ps", "-q", "--status", "running", "relay"):
                return None
            return compose("logs", "--no-color", "relay")

        wait_until_ready(base_url, exit_log)
        yield LiveRelay(base_url, database_url)
    finally:
        # -v removes this project's Postgres volume as well.
        subprocess.run(
            ["docker", "compose", "-p", project, "down", "-v", "--remove-orphans"],
            cwd=PROJECT_DIR, env=env, capture_output=True, timeout=120,
        )


def run_checked(*cmd: str, timeout: float = 120) -> str:
    result = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        pytest.fail(f"{' '.join(cmd)} failed:\n{result.stdout}{result.stderr}")
    return result.stdout.strip()


def start_port_forward(
    kubectl: list[str], target: str, remote_port: int, log_path: Path
) -> tuple[subprocess.Popen, int]:
    local_port = free_port()
    # port-forward logs a line per connection; send it to a file, since an
    # undrained pipe would eventually fill and stall the forward.
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [*kubectl, "port-forward", target, f"{local_port}:{remote_port}"],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    return proc, local_port


def wait_for_port(port: int, proc: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + 30
    while True:
        if proc.poll() is not None:
            pytest.fail(f"port-forward exited:\n{log_path.read_text(errors='replace')}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            if time.monotonic() > deadline:
                pytest.fail(f"port-forward to {port} did not open within 30 seconds")
            time.sleep(0.2)


@pytest.fixture(scope="module")
def kubernetes_relay(tmp_path_factory):
    cluster = os.getenv("RELAY_TEST_KIND_CLUSTER", "kind")
    if not docker_available() or shutil.which("kind") is None or shutil.which("kubectl") is None:
        target_unavailable("kind, kubectl, or Docker is not available")
    if cluster not in subprocess.run(["kind", "get", "clusters"], capture_output=True, text=True).stdout.split():
        target_unavailable(f"kind cluster {cluster!r} does not exist (set RELAY_TEST_KIND_CLUSTER)")

    suffix = uuid.uuid4().hex[:8]
    namespace = f"relay-it-{suffix}"
    image_tag = f"it-{suffix}"
    password = uuid.uuid4().hex
    kubectl = ["kubectl", "--context", f"kind-{cluster}", "-n", namespace]

    # Overlay on a copy of k8s/: own namespace, own image tag, own password,
    # so neither the cluster's agent-relay namespace nor agent-relay:local on
    # the node is touched, and the gitignored k8s/postgres.env is not needed.
    overlay = tmp_path_factory.mktemp("k8s-overlay")
    shutil.copytree(PROJECT_DIR / "k8s", overlay / "base", ignore=shutil.ignore_patterns("postgres.env"))
    (overlay / "base" / "postgres.env").write_text(f"POSTGRES_PASSWORD={password}\n")
    (overlay / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        f"namespace: {namespace}\n"
        "resources:\n"
        "  - base\n"
        "images:\n"
        "  - name: agent-relay\n"
        f"    newTag: {image_tag}\n"
    )

    image = f"agent-relay:{image_tag}"
    forwards: list[subprocess.Popen] = []
    try:
        run_checked("docker", "build", "-q", "-t", image, str(PROJECT_DIR), timeout=600)
        run_checked("kind", "load", "docker-image", image, "--name", cluster, timeout=600)
        run_checked("kubectl", "--context", f"kind-{cluster}", "apply", "-k", str(overlay))
        for workload in ("statefulset/postgres", "deployment/relay"):
            rollout = subprocess.run(
                [*kubectl, "rollout", "status", workload, "--timeout=240s"],
                capture_output=True, text=True, timeout=300,
            )
            if rollout.returncode != 0:
                state = subprocess.run(
                    [*kubectl, "describe", "pods"], capture_output=True, text=True, timeout=60
                ).stdout
                pytest.fail(f"{workload} did not roll out:\n{rollout.stderr}\n{state}")

        relay_log, pg_log = overlay / "relay-forward.log", overlay / "postgres-forward.log"
        relay_forward, relay_port = start_port_forward(kubectl, "svc/relay", 8000, relay_log)
        forwards.append(relay_forward)
        pg_forward, pg_port = start_port_forward(kubectl, "pod/postgres-0", 5432, pg_log)
        forwards.append(pg_forward)
        wait_for_port(relay_port, relay_forward, relay_log)
        wait_for_port(pg_port, pg_forward, pg_log)

        base_url = f"http://127.0.0.1:{relay_port}"
        wait_until_ready(
            base_url,
            lambda: relay_log.read_text(errors="replace") if relay_forward.poll() is not None else None,
        )
        yield LiveRelay(base_url, f"postgresql://relay:{password}@127.0.0.1:{pg_port}/relay")
    finally:
        for proc in forwards:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        # Deleting the namespace also deletes the PVC and its volume.
        subprocess.run(
            ["kubectl", "--context", f"kind-{cluster}", "delete", "namespace", namespace, "--wait=true"],
            capture_output=True, timeout=300,
        )
        # kind has no "unload"; remove the test tag from each node directly.
        # Not `crictl rmi`: it deletes the image by ID with *all* its tags,
        # and the test image is usually identical to agent-relay:local, which
        # a long-running deployment on the node still needs.
        nodes = subprocess.run(["kind", "get", "nodes", "--name", cluster], capture_output=True, text=True)
        for node in nodes.stdout.split():
            subprocess.run(
                ["docker", "exec", node, "ctr", "-n", "k8s.io", "images", "rm", f"docker.io/library/{image}"],
                capture_output=True, timeout=60,
            )
        subprocess.run(["docker", "rmi", image], capture_output=True, timeout=60)


@pytest.fixture(params=["process", "compose", "kubernetes"])
def live_relay(request) -> LiveRelay:
    return request.getfixturevalue(f"{request.param}_relay")


def dump_table_data(conn: psycopg.Connection, table: str) -> bytes:
    buf = bytearray()
    with conn.cursor().copy(f"COPY {table} TO STDOUT") as copy:
        for chunk in copy:
            buf += chunk
    return bytes(buf)


def test_scenario_1_send_claim_complete_read_over_http(live_relay):
    base_url = live_relay.base_url
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

    # The outcome is persisted in PostgreSQL, with secrets stored only as hashes.
    secrets = [sender["token"], recipient["token"], claimed["claim_token"]]
    with psycopg.connect(live_relay.database_url) as db:
        status, stored_output, sender_id, recipient_id, attempt_count = db.execute(
            "SELECT status, output, sender_id, recipient_id, attempt_count FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
        assert (status, stored_output, attempt_count) == ("completed", output, 1)
        assert (sender_id, recipient_id) == (sender["agent_id"], recipient["agent_id"])

        outcome, claim_token_hash = db.execute(
            "SELECT outcome, claim_token_hash FROM attempts WHERE task_id = %s", (task_id,)
        ).fetchone()
        assert outcome == "completed"
        assert claim_token_hash == hashlib.sha256(claimed["claim_token"].encode()).hexdigest()

        for agent in (sender, recipient):
            [token_hash] = db.execute(
                "SELECT token_hash FROM agents WHERE id = %s", (agent["agent_id"],)
            ).fetchone()
            assert token_hash == hashlib.sha256(agent["token"].encode()).hexdigest()

        for table in ("agents", "tasks", "attempts"):
            raw = dump_table_data(db, table)
            for secret in secrets:
                assert secret.encode() not in raw, f"plaintext secret found in {table}"
