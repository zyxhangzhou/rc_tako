"""从环境变量加载运行配置。"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

ENV_DB_PATH = "NOTIFIER_DB_PATH"
ENV_TOKENS_FILE = "NOTIFIER_TOKENS_FILE"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    db_path: Path
    # token -> 调用方名称
    tokens: dict[str, str]


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    db_path = env.get(ENV_DB_PATH)
    tokens_file = env.get(ENV_TOKENS_FILE)
    missing = [name for name, value in ((ENV_DB_PATH, db_path), (ENV_TOKENS_FILE, tokens_file)) if not value]
    if missing:
        raise ConfigError(f"缺少必需的环境变量: {', '.join(missing)}")

    try:
        raw = json.loads(Path(tokens_file).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"token 文件不存在: {tokens_file}") from None
    except json.JSONDecodeError as e:
        raise ConfigError(f"token 文件不是合法 JSON: {tokens_file}: {e}") from None

    if not isinstance(raw, dict) or not raw or not all(
        isinstance(k, str) and k and isinstance(v, str) and v for k, v in raw.items()
    ):
        raise ConfigError(f"token 文件必须是非空的 {{token: 调用方名称}} 映射: {tokens_file}")

    return Settings(db_path=Path(db_path), tokens=raw)
