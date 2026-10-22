import pytest
from fastapi.testclient import TestClient

from notifier import store
from notifier.app import create_app
from notifier.config import Settings

TOKENS = {"t-crm": "crm-service", "t-ads": "ads-service"}


def public_resolver(host):
    return ["93.184.216.34"]


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "n.db", tokens=TOKENS)


@pytest.fixture
def app(settings):
    return create_app(settings, resolver=public_resolver)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(settings):
    conn = store.connect(settings.db_path)
    yield conn
    conn.close()


def auth(token="t-crm"):
    return {"Authorization": f"Bearer {token}"}


def payload(**overrides):
    data = dict(
        url="https://api.example.com/hook",
        method="POST",
        headers={"Content-Type": "application/json"},
        body='{"event": "registered"}',
        idempotency_key="evt-1",
    )
    data.update(overrides)
    return data


def row_count(conn):
    return conn.execute("SELECT count(*) FROM notifications").fetchone()[0]
