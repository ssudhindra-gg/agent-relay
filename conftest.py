"""Provision a throwaway PostgreSQL server for the test session.

The relay's engine is created when :mod:`database` is imported, so the test
database must exist before any test module is collected.  ``pytest_configure``
runs first and either:

- uses the server named by ``RELAY_TEST_POSTGRES_URL`` (an admin URL such as
  ``postgresql://postgres:secret@localhost:5432/postgres``), or
- starts a disposable ``postgres:17-alpine`` container on a random port.

Tests then run against freshly created databases on that server.  They never
read ``RELAY_DATABASE_URL``, so a developer's real database cannot be dropped
by accident.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid

import psycopg
import pytest
from psycopg import sql

POSTGRES_IMAGE = "postgres:17-alpine"
_admin_url: str | None = None
_container_id: str | None = None


def _start_container() -> str:
    global _container_id
    password = uuid.uuid4().hex
    result = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "-e", f"POSTGRES_PASSWORD={password}",
            "-p", "127.0.0.1::5432",
            POSTGRES_IMAGE,
        ],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise pytest.UsageError(
            "Tests need PostgreSQL: start Docker or set RELAY_TEST_POSTGRES_URL.\n" + result.stderr
        )
    _container_id = result.stdout.strip()
    port = subprocess.run(
        ["docker", "port", _container_id, "5432/tcp"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0].rsplit(":", 1)[1]
    return f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"


def _wait_for(url: str) -> None:
    # The image's init phase accepts only socket connections and then
    # restarts, so wait until a real TCP query succeeds.
    deadline = time.monotonic() + 60
    while True:
        try:
            with psycopg.connect(url, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def create_database(admin_url: str, prefix: str) -> str:
    """Create an empty database and return its SQLAlchemy URL."""

    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    base, _, _ = admin_url.rpartition("/")
    return f"{base.replace('postgresql://', 'postgresql+psycopg://', 1)}/{name}"


def drop_database(admin_url: str, url: str) -> None:
    name = url.rsplit("/", 1)[1]
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def pytest_configure(config):
    global _admin_url
    _admin_url = os.getenv("RELAY_TEST_POSTGRES_URL") or _start_container()
    _wait_for(_admin_url)
    # Consumed by database.py at import time for the in-process tests.
    os.environ["RELAY_DATABASE_URL"] = create_database(_admin_url, "relay_test")
    os.environ.pop("DATABASE_URL", None)


def pytest_unconfigure(config):
    if _container_id is not None:
        subprocess.run(["docker", "rm", "-f", "-v", _container_id], capture_output=True, timeout=60)
    elif _admin_url is not None and "RELAY_DATABASE_URL" in os.environ:
        drop_database(_admin_url, os.environ["RELAY_DATABASE_URL"])


@pytest.fixture(scope="session")
def postgres_admin_url() -> str:
    assert _admin_url is not None
    return _admin_url
