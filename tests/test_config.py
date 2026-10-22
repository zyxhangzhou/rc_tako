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
