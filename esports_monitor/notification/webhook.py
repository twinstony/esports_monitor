"""Webhook 通知。

支持多个 webhook 地址，通过 HTTP POST 发送 JSON body。
body 格式：
{
    "event": "trade_opened" | "trade_settled" | "signal_alert",
    "timestamp": "2026-07-14T13:54:16Z",
    "details": { ... }
}
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from ..utils.time_utils import now_utc, to_utc_iso


class WebhookNotifier:
    """多 webhook 通知器。

    所有方法均不抛异常——网络错误返回 False，保证主循环不被中断。
    """

    def __init__(
        self,
        urls: Optional[List[str]] = None,
        enabled: bool = True,
        timeout: int = 10,
        logger: Optional[logging.Logger] = None,
    ):
        self.urls = urls or []
        self.enabled = enabled and len(self.urls) > 0
        self.timeout = timeout
        self._logger = logger or logging.getLogger(__name__)

    def can_send(self) -> bool:
        return self.enabled and len(self.urls) > 0

    def _post(self, payload: Dict[str, Any]) -> bool:
        """向所有 webhook 地址发送 POST 请求。"""
        if not self.can_send():
            return False
        ok = True
        for url in self.urls:
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout)
                if resp.status_code not in (200, 201, 202, 204):
                    self._logger.error(
                        "Webhook 发送失败 url=%s status=%s body=%s",
                        url, resp.status_code, resp.text[:200],
                    )
                    ok = False
            except requests.exceptions.RequestException as exc:
                self._logger.error("Webhook 发送异常 url=%s: %s", url, exc)
                ok = False
        return ok

    def send_trade_opened(self, trade_data: Dict[str, Any]) -> bool:
        """发送模拟开单通知。

        trade_data 应包含：
            match_id, slug, game, team_a, team_b,
            signal_name, window_label, buy_team, buy_price,
            quantity, notional_usd, vwap, opened_at
        """
        payload = {
            "event": "trade_opened",
            "timestamp": to_utc_iso(now_utc()),
            "details": trade_data,
        }
        return self._post(payload)

    def send_trade_settled(self, settle_data: Dict[str, Any]) -> bool:
        """发送模拟结算通知。

        settle_data 应包含：
            trade_id, match_id, game, buy_team, winning_team,
            buy_price, pnl_usd, settled_at
        """
        payload = {
            "event": "trade_settled",
            "timestamp": to_utc_iso(now_utc()),
            "details": settle_data,
        }
        return self._post(payload)

    def send_signal_alert(self, signal_data: Dict[str, Any]) -> bool:
        """发送形态信号告警。

        signal_data 应包含：
            match_id, slug, game, team_a, team_b,
            signal_name, window_label, buy_team, buy_price,
            signal_strength, minutes_since_start
        """
        payload = {
            "event": "signal_alert",
            "timestamp": to_utc_iso(now_utc()),
            "details": signal_data,
        }
        return self._post(payload)


def create_webhook_notifier_from_config(
    config: Dict[str, Any],
    logger: Optional[logging.Logger] = None,
) -> WebhookNotifier:
    """从配置创建 WebhookNotifier。

    配置格式（config.yaml）：
        notification:
          webhook:
            enabled: true
            urls:
              - https://example.com/webhook/trade
            timeout: 10
    """
    wh_cfg = (config.get("notification") or {}).get("webhook") or {}
    return WebhookNotifier(
        urls=wh_cfg.get("urls") or [],
        enabled=bool(wh_cfg.get("enabled", False)),
        timeout=int(wh_cfg.get("timeout", 10)),
        logger=logger,
    )
