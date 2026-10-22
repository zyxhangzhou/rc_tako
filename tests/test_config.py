import json

import pytest

from notifier.config import ENV_DB_PATH, ENV_TOKENS_FILE, ConfigError, load_settings


def test_loads_db_path_and_tokens(tmp_path):
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"t-crm": "crm-service"}))
    s = load_settings({ENV_DB_PATH: str(tmp_path / "n.db"), ENV_TOKENS_FILE: str(tokens)})
    assert s.db_path == tmp_path / "n.db"
    assert s.tokens == {"t-crm": "crm-service"}


@pytest.mark.parametrize("drop", [ENV_DB_PATH, ENV_TOKENS_FILE])
def test_missing_env_var_names_it(tmp_path, drop):
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"t": "a"}))
    env = {ENV_DB_PATH: str(tmp_path / "n.db"), ENV_TOKENS_FILE: str(tokens)}
    del env[drop]
    with pytest.raises(ConfigError, match=drop):
        load_settings(env)


def test_missing_tokens_file(tmp_path):
    with pytest.raises(ConfigError, match="不存在"):
        load_settings({ENV_DB_PATH: "x.db", ENV_TOKENS_FILE: str(tmp_path / "nope.json")})


@pytest.mark.parametrize("content", ["not json", "[]", "{}", '{"t": ""}'])
def test_malformed_tokens_file(tmp_path, content):
    tokens = tmp_path / "tokens.json"
    tokens.write_text(content)
    with pytest.raises(ConfigError):
        load_settings({ENV_DB_PATH: "x.db", ENV_TOKENS_FILE: str(tokens)})


# --- worker 配置 ---

from notifier.config import WorkerSettings, load_worker_settings  # noqa: E402


def test_worker_defaults_need_only_db_path():
    s = load_worker_settings({ENV_DB_PATH: "n.db"})
    assert s == WorkerSettings(db_path=s.db_path)
    assert (s.concurrency, s.lease_s, s.request_timeout_s, s.max_retry_duration_s) == (16, 60.0, 10.0, 86400.0)


def test_worker_overrides():
    s = load_worker_settings(
        {ENV_DB_PATH: "n.db", "NOTIFIER_WORKER_CONCURRENCY": "4", "NOTIFIER_WORKER_LEASE_S": "2", "NOTIFIER_WORKER_REQUEST_TIMEOUT_S": "0.5"}
    )
    assert (s.concurrency, s.lease_s, s.request_timeout_s) == (4, 2.0, 0.5)


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"NOTIFIER_WORKER_CONCURRENCY": "x"},
        {"NOTIFIER_WORKER_CONCURRENCY": "1.5"},
        {"NOTIFIER_WORKER_POLL_INTERVAL_S": "0"},
        {"NOTIFIER_WORKER_LEASE_S": "10"},  # 不大于默认请求超时 10s
        {"NOTIFIER_WORKER_BACKOFF_CAP_S": "5"},  # 小于默认首次间隔 10s
    ],
)
def test_worker_invalid(env):
    full = {ENV_DB_PATH: "n.db", **env} if env else {}
    with pytest.raises(ConfigError):
        load_worker_settings(full)
