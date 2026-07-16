"""配置管理：分层配置加载/合并/机密管理/日志配置。

分层结构（优先级从低到高）：
    DEFAULT_CONFIG (代码内置默认值)
        ← config.yaml (主配置，版本控制，非机密)
            ← config.local.yaml (本地覆盖，机密，gitignore)
                ← 环境变量 (机密，部署用)

参照：polymarket_twitter_monitor/config/manager.py
"""
from __future__ import annotations

import copy
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional, Tuple

import yaml

# 配置文件常量
CONFIG_FILE = "config.yaml"
LOCAL_CONFIG_FILE = "config.local.yaml"

# 机密字段映射：(section, key) -> 环境变量名
SECRET_ENV_MAP: Dict[Tuple[str, str, str], str] = {
    ("notification", "telegram", "bot_token"): "ESPORTS_MONITOR_TELEGRAM_BOT_TOKEN",
    ("notification", "telegram", "chat_id"): "ESPORTS_MONITOR_TELEGRAM_CHAT_ID",
}

# 机密字段的路径（section, key 列表），用于 save() 时拆分到 config.local.yaml
SECRET_PATHS = [
    ("notification", "telegram", "bot_token"),
    ("notification", "telegram", "chat_id"),
]


# 代码内置默认配置（最低优先级）
DEFAULT_CONFIG: Dict[str, Any] = {
    "polymarket": {
        "gamma_api_base": "https://gamma-api.polymarket.com",
        "clob_api_base": "https://clob.polymarket.com",
        "timeout": 10,
    },
    "discovery": {
        "enabled": True,
        # 默认仅监控 LoL（当前业务目标：先确保 LoL 数据准确）
        "games": ["lol"],
        "tag_ids": {"lol": ""},
        "tag_slugs": ["lol", "league-of-legends"],
        "tag_slug": "esports",
        "leagues": ["esports-world-cup"],
        "min_depth": 50,
        "max_spread": 0.05,
        "price_min": 0.05,
        "price_max": 0.95,
        "interval_hours": 6,
        "lookahead_count": 20,
        "timeout": 30,
    },
    "polling": {
        "interval_seconds": 30,
        "idle_interval_seconds": 300,
    },
    "morphology": {
        "enabled": True,
        "cooldown_minutes": 15,
        "resample_points": 48,
        "window_hours": 2.0,
        "buy_price_min": 0.05,
        "buy_price_max": 0.95,
        "observe_all": True,
        "backtest_dir": "docs/backtest_results",
        "simulation": {
            "enabled": True,
            "notional_usd": 100,
            "use_depth": True,
        },
        "windows": [
            {"label": "early", "min_minutes": 0, "max_minutes": 30,
             "rules": ["current_and_recent_leader", "stable_spread"]},
            {"label": "mid", "min_minutes": 30, "max_minutes": 90,
             "rules": ["breakout", "momentum_catcher", "divergence"]},
            {"label": "late", "min_minutes": 90, "max_minutes": 9999,
             "rules": ["stable_spread", "current_and_recent_leader"]},
        ],
    },
    "esports_data": {"enabled": False, "provider": None},
    "notification": {
        "telegram": {
            "enabled": True,
            "bot_token": "",
            "chat_id": "",
            "status_report_enabled": True,
            "status_report_interval_hours": 6,
        }
    },
    "retention": {
        "days": 90,
        "archive_dir": "archives",
        "archive_interval_days": 7,
        "vacuum_enabled": True,
        "compress": True,
    },
    "database": {"path": "esports_history.db"},
    "logging": {
        "level": "INFO",
        "file": "esports_monitor.log",
        "max_bytes": 10485760,
        "backup_count": 5,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并字典：override 中的非字典值覆盖 base，字典则递归合并。

    修改 base 原地，同时返回 base。
    """
    for key, value in (override or {}).items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _apply_env_overrides(config: Dict[str, Any]) -> Dict[str, Any]:
    """从环境变量中读取机密覆盖到 config。

    环境变量优先级最高。
    """
    for path, env_name in SECRET_ENV_MAP.items():
        env_value = os.environ.get(env_name)
        if env_value is None:
            continue
        # path 形如 (section, subsection, key)
        cursor = config
        for p in path[:-1]:
            if not isinstance(cursor.get(p), dict):
                cursor[p] = {}
            cursor = cursor[p]
        cursor[path[-1]] = env_value

    # 部署相关环境变量（Docker / 云平台）
    db_path = os.environ.get("DB_PATH")
    if db_path:
        config.setdefault("database", {})["path"] = db_path

    return config


def _load_yaml_file(path: str) -> Dict[str, Any]:
    """读取 YAML 文件；失败返回空字典。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError) as exc:
        logging.getLogger(__name__).error("Failed to load %s: %s", path, exc)
        return {}


def load_config(base_dir: Optional[str] = None) -> Dict[str, Any]:
    """加载并合并配置。

    优先级：DEFAULT_CONFIG < config.yaml < config.local.yaml < 环境变量

    Args:
        base_dir: 配置文件所在目录。None 表示当前工作目录。

    Returns:
        合并后的配置字典。
    """
    base = copy.deepcopy(DEFAULT_CONFIG)
    base_dir = base_dir or os.getcwd()

    # config.yaml
    yaml_path = os.path.join(base_dir, CONFIG_FILE)
    _deep_merge(base, _load_yaml_file(yaml_path))

    # config.local.yaml
    local_path = os.path.join(base_dir, LOCAL_CONFIG_FILE)
    _deep_merge(base, _load_yaml_file(local_path))

    # 环境变量
    _apply_env_overrides(base)

    return base


def save_config(config: Dict[str, Any], base_dir: Optional[str] = None) -> bool:
    """保存配置：机密写入 config.local.yaml，其余写入 config.yaml。

    Args:
        config: 完整配置字典
        base_dir: 配置文件所在目录

    Returns:
        成功返回 True，失败返回 False。
    """
    base_dir = base_dir or os.getcwd()
    try:
        base_payload = copy.deepcopy(config)
        local_payload: Dict[str, Any] = {}

        # 拆分机密字段到 local_payload
        for path in SECRET_PATHS:
            cursor = base_payload
            local_cursor = local_payload
            for p in path[:-1]:
                if not isinstance(cursor.get(p), dict):
                    break
                cursor = cursor[p]
                local_cursor = local_cursor.setdefault(p, {})
            else:
                value = cursor.get(path[-1], "")
                cursor[path[-1]] = ""  # 从主配置中清除
                if value:
                    local_cursor[path[-1]] = value

        yaml_path = os.path.join(base_dir, CONFIG_FILE)
        local_path = os.path.join(base_dir, LOCAL_CONFIG_FILE)

        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(base_payload, f, allow_unicode=True, sort_keys=False)

        if local_payload:
            with open(local_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(local_payload, f, allow_unicode=True, sort_keys=False)

        return True
    except (OSError, yaml.YAMLError) as exc:
        logging.getLogger(__name__).error("Failed to save config: %s", exc)
        return False


def config_file_signature(base_dir: Optional[str] = None) -> Tuple[Optional[float], Optional[float]]:
    """获取配置文件的 mtime 签名，用于热重载检测。

    Returns:
        (config.yaml mtime, config.local.yaml mtime)；文件不存在返回 None。
    """
    base_dir = base_dir or os.getcwd()
    signature: list = [None, None]
    for idx, name in enumerate((CONFIG_FILE, LOCAL_CONFIG_FILE)):
        path = os.path.join(base_dir, name)
        try:
            if os.path.exists(path):
                signature[idx] = os.path.getmtime(path)
        except OSError:
            signature[idx] = None
    return signature[0], signature[1]


def get_config_value(config: Dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    """通过点号路径访问嵌套配置。

    例如 get_config_value(config, "morphology.enabled", False)
    """
    cursor = config
    for part in dotted_key.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


def setup_logging(config: Dict[str, Any]) -> None:
    """配置日志：RotatingFileHandler + StreamHandler。

    强制 stdout/stderr UTF-8（Windows 兼容）。
    抑制高频第三方库日志。
    """
    log_config = config.get("logging", {}) or {}

    # Windows 下强制 UTF-8 输出
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

    log_file = log_config.get("file", "esports_monitor.log")
    max_bytes = int(log_config.get("max_bytes", 10485760))
    backup_count = int(log_config.get("backup_count", 5))
    level_name = str(log_config.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    root = logging.getLogger()
    # 移除现有 handler，避免重复
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)

    # 文件 handler
    try:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        root.addHandler(file_handler)
    except (OSError, PermissionError) as exc:
        # 文件日志失败不影响控制台日志
        logging.getLogger(__name__).warning("Cannot open log file %s: %s", log_file, exc)

    # 控制台 handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(level)
    root.addHandler(console)

    # 抑制高频第三方库日志
    for lib in ("httpx", "httpcore", "urllib3", "requests", "asyncio"):
        logging.getLogger(lib).setLevel(logging.WARNING)
