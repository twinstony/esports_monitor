"""FastAPI 依赖注入。

初始化 storage / repository / morphology 组件，供 API 路由使用。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from ..data.sqlite_storage import SQLiteStorage
from ..morphology.repository import MorphologyRepository
from ..morphology.signals import load_backtest_stats
from ..morphology.simulator import MorphologySimulator
from ..config.manager import load_config


class WebDependencies:
    """Web 层依赖容器。

    在 FastAPI 启动时初始化一次，通过 app.state 注入到各路由。
    """

    def __init__(self, base_dir: Optional[str] = None):
        self._base_dir = base_dir or os.getcwd()
        self._logger = logging.getLogger(__name__)

        # 加载配置
        self.config = load_config(self._base_dir)

        # 数据库
        db_path = (self.config.get("database") or {}).get("path", "esports_history.db")
        if self._base_dir and not os.path.isabs(db_path):
            db_path = os.path.join(self._base_dir, db_path)
        self.storage = SQLiteStorage(db_path=db_path)

        # 形态分析
        morph_cfg = self.config.get("morphology") or {}
        self.morphology_repo = MorphologyRepository(self.storage, logger=self._logger)

        backtest_dir = morph_cfg.get("backtest_dir") or "docs/backtest_results"
        if self._base_dir and not os.path.isabs(backtest_dir):
            backtest_dir = os.path.join(self._base_dir, backtest_dir)
        self.backtest_stats = load_backtest_stats(backtest_dir)

        self.morphology_simulator = MorphologySimulator(
            repository=self.morphology_repo,
            config=morph_cfg,
            logger=self._logger,
        )

        self._logger.info("Web 依赖初始化完成 (db=%s)", db_path)

    @property
    def base_dir(self) -> str:
        return self._base_dir
