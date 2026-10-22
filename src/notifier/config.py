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


@dataclass(frozen=True)
class WorkerSettings:
    db_path: Path
    concurrency: int = 16
    per_host_limit: int = 4
    poll_interval_s: float = 1.0
    lease_s: float = 60.0
    request_timeout_s: float = 10.0
    backoff_base_s: float = 10.0
    backoff_cap_s: float = 3600.0
    max_retry_duration_s: float = 24 * 3600.0


# 环境变量名 -> (字段名, 类型)
_WORKER_ENV = {
    "NOTIFIER_WORKER_CONCURRENCY": ("concurrency", int),
    "NOTIFIER_WORKER_PER_HOST_LIMIT": ("per_host_limit", int),
    "NOTIFIER_WORKER_POLL_INTERVAL_S": ("poll_interval_s", float),
    "NOTIFIER_WORKER_LEASE_S": ("lease_s", float),
    "NOTIFIER_WORKER_REQUEST_TIMEOUT_S": ("request_timeout_s", float),
    "NOTIFIER_WORKER_BACKOFF_BASE_S": ("backoff_base_s", float),
    "NOTIFIER_WORKER_BACKOFF_CAP_S": ("backoff_cap_s", float),
    "NOTIFIER_WORKER_MAX_RETRY_DURATION_S": ("max_retry_duration_s", float),
}


def load_worker_settings(env: dict[str, str] | None = None) -> WorkerSettings:
    """worker 只需要数据库路径，不读 token 文件。"""
    env = os.environ if env is None else env
    db_path = env.get(ENV_DB_PATH)
    if not db_path:
        raise ConfigError(f"缺少必需的环境变量: {ENV_DB_PATH}")

    overrides = {}
    for name, (field, typ) in _WORKER_ENV.items():
        raw = env.get(name)
        if raw is None:
            continue
        try:
            value = typ(raw)
        except ValueError:
            raise ConfigError(f"{name} 必须是{'整数' if typ is int else '数字'}: {raw!r}") from None
        if value <= 0:
            raise ConfigError(f"{name} 必须大于 0: {raw!r}")
        overrides[field] = value

    s = WorkerSettings(db_path=Path(db_path), **overrides)
    # 占用期限必须覆盖一次完整请求，否则请求还没结束通知就会被别人重新取走
    if s.lease_s <= s.request_timeout_s:
        raise ConfigError("NOTIFIER_WORKER_LEASE_S 必须大于 NOTIFIER_WORKER_REQUEST_TIMEOUT_S")
    if s.per_host_limit > s.concurrency:
        raise ConfigError("NOTIFIER_WORKER_PER_HOST_LIMIT 不能大于 NOTIFIER_WORKER_CONCURRENCY")
    if s.backoff_cap_s < s.backoff_base_s:
        raise ConfigError("NOTIFIER_WORKER_BACKOFF_CAP_S 不能小于 NOTIFIER_WORKER_BACKOFF_BASE_S")
    return s
