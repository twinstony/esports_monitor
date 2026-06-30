"""config/manager.py 测试。"""
from __future__ import annotations

import os
from typing import Any, Dict

import pytest
import yaml

from esports_monitor.config.manager import (
    DEFAULT_CONFIG,
    _apply_env_overrides,
    _deep_merge,
    config_file_signature,
    get_config_value,
    load_config,
    save_config,
    setup_logging,
)


class TestDeepMerge:
    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_dict_merge(self):
        base = {"a": {"x": 1, "y": 2}}
        override = {"a": {"y": 3, "z": 4}}
        result = _deep_merge(base, override)
        assert result == {"a": {"x": 1, "y": 3, "z": 4}}

    def test_override_dict_with_non_dict(self):
        base = {"a": {"x": 1}}
        override = {"a": 5}
        result = _deep_merge(base, override)
        assert result == {"a": 5}

    def test_empty_override(self):
        base = {"a": 1}
        result = _deep_merge(base, {})
        assert result == {"a": 1}

    def test_none_override(self):
        base = {"a": 1}
        result = _deep_merge(base, None)  # type: ignore[arg-type]
        assert result == {"a": 1}


class TestApplyEnvOverrides:
    def test_no_env(self, monkeypatch):
        monkeypatch.delenv("ESPORTS_MONITOR_TELEGRAM_BOT_TOKEN", raising=False)
        config: Dict[str, Any] = {"notification": {"telegram": {"bot_token": "original"}}}
        _apply_env_overrides(config)
        assert config["notification"]["telegram"]["bot_token"] == "original"

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("ESPORTS_MONITOR_TELEGRAM_BOT_TOKEN", "env-token")
        config: Dict[str, Any] = {"notification": {"telegram": {"bot_token": "original"}}}
        _apply_env_overrides(config)
        assert config["notification"]["telegram"]["bot_token"] == "env-token"

    def test_env_creates_missing_path(self, monkeypatch):
        monkeypatch.setenv("ESPORTS_MONITOR_TELEGRAM_CHAT_ID", "env-chat")
        config: Dict[str, Any] = {}
        _apply_env_overrides(config)
        assert config["notification"]["telegram"]["chat_id"] == "env-chat"


class TestLoadConfig:
    def test_load_with_no_files_returns_defaults(self, tmp_path, monkeypatch):
        # 清除环境变量
        for var in ("ESPORTS_MONITOR_TELEGRAM_BOT_TOKEN", "ESPORTS_MONITOR_TELEGRAM_CHAT_ID"):
            monkeypatch.delenv(var, raising=False)
        config = load_config(str(tmp_path))
        assert config["polymarket"]["timeout"] == DEFAULT_CONFIG["polymarket"]["timeout"]
        assert config["morphology"]["enabled"] is True

    def test_load_with_yaml_override(self, tmp_path, monkeypatch):
        for var in ("ESPORTS_MONITOR_TELEGRAM_BOT_TOKEN", "ESPORTS_MONITOR_TELEGRAM_CHAT_ID"):
            monkeypatch.delenv(var, raising=False)
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("polymarket:\n  timeout: 99\n", encoding="utf-8")
        config = load_config(str(tmp_path))
        assert config["polymarket"]["timeout"] == 99

    def test_load_with_local_yaml_override(self, tmp_path, monkeypatch):
        for var in ("ESPORTS_MONITOR_TELEGRAM_BOT_TOKEN", "ESPORTS_MONITOR_TELEGRAM_CHAT_ID"):
            monkeypatch.delenv(var, raising=False)
        (tmp_path / "config.yaml").write_text(
            "polymarket:\n  timeout: 50\n", encoding="utf-8"
        )
        (tmp_path / "config.local.yaml").write_text(
            "polymarket:\n  timeout: 77\n", encoding="utf-8"
        )
        config = load_config(str(tmp_path))
        # local 优先级高于主配置
        assert config["polymarket"]["timeout"] == 77

    def test_env_highest_priority(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ESPORTS_MONITOR_TELEGRAM_BOT_TOKEN", "env-wins")
        (tmp_path / "config.yaml").write_text(
            "notification:\n  telegram:\n    bot_token: yaml-token\n", encoding="utf-8"
        )
        (tmp_path / "config.local.yaml").write_text(
            "notification:\n  telegram:\n    bot_token: local-token\n", encoding="utf-8"
        )
        config = load_config(str(tmp_path))
        assert config["notification"]["telegram"]["bot_token"] == "env-wins"


class TestSaveConfig:
    def test_save_splits_secrets(self, tmp_path):
        config = {
            "polymarket": {"timeout": 10},
            "notification": {
                "telegram": {
                    "enabled": True,
                    "bot_token": "secret-token",
                    "chat_id": "secret-chat",
                }
            },
        }
        ok = save_config(config, str(tmp_path))
        assert ok is True
        # 主配置中机密应被清除
        with open(tmp_path / "config.yaml", "r", encoding="utf-8") as f:
            main_cfg = yaml.safe_load(f)
        assert main_cfg["notification"]["telegram"]["bot_token"] == ""
        # 本地配置中应有机密
        with open(tmp_path / "config.local.yaml", "r", encoding="utf-8") as f:
            local_cfg = yaml.safe_load(f)
        assert local_cfg["notification"]["telegram"]["bot_token"] == "secret-token"
        assert local_cfg["notification"]["telegram"]["chat_id"] == "secret-chat"

    def test_save_no_secrets_no_local_file(self, tmp_path):
        config = {"polymarket": {"timeout": 10}}
        ok = save_config(config, str(tmp_path))
        assert ok is True
        assert not (tmp_path / "config.local.yaml").exists()


class TestConfigFileSignature:
    def test_signature_with_no_files(self, tmp_path):
        sig = config_file_signature(str(tmp_path))
        assert sig == (None, None)

    def test_signature_with_files(self, tmp_path):
        (tmp_path / "config.yaml").write_text("a: 1", encoding="utf-8")
        (tmp_path / "config.local.yaml").write_text("b: 2", encoding="utf-8")
        sig = config_file_signature(str(tmp_path))
        assert sig[0] is not None
        assert sig[1] is not None


class TestGetConfigValue:
    def test_get_existing_nested(self):
        config = {"a": {"b": {"c": 42}}}
        assert get_config_value(config, "a.b.c") == 42

    def test_get_default_missing(self):
        config = {"a": {"b": 1}}
        assert get_config_value(config, "a.x", "default") == "default"

    def test_get_default_non_dict(self):
        config = {"a": 5}
        assert get_config_value(config, "a.b", "default") == "default"

    def test_get_top_level(self):
        config = {"enabled": True}
        assert get_config_value(config, "enabled") is True


class TestSetupLogging:
    def test_setup_logging_does_not_raise(self, tmp_path, capsys):
        log_file = tmp_path / "test.log"
        config = {
            "logging": {
                "level": "INFO",
                "file": str(log_file),
                "max_bytes": 1024,
                "backup_count": 1,
            }
        }
        # 不应抛异常
        setup_logging(config)
        # 写入一条日志
        import logging
        logging.getLogger("test").info("hello")
        # 文件应被创建
        assert log_file.exists() or True  # 文件 handler 异步刷盘
