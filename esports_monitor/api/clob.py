"""CLOB API 客户端（盘口深度）。

参照：polymarket_twitter_monitor/api/clob.py

CLOB API（https://clob.polymarket.com）是公开只读端点，用于获取实时盘口深度。

映射链：condition_id → token_id → orderbook

token_id 缓存机制：
- 优先从 clob_token_cache 表读取
- 缓存未命中时调 Gamma API 获取（注意：用 Gamma 而非 CLOB 的 /markets，
  因为 CLOB 的 /markets 不支持 condition_id 过滤）
- 获取后写入缓存表
- 404 时自动清除缓存并重试
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests


class ClobClient:
    """CLOB API 客户端。

    所有公开方法均不抛异常——网络/解析错误返回 None 或空值，
    保证主循环不被中断。
    """

    BASE_URL = "https://clob.polymarket.com"
    GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

    def __init__(
        self,
        storage: Any = None,
        api_base: Optional[str] = None,
        gamma_api_base: Optional[str] = None,
        timeout: float = 10.0,
        max_retries: int = 2,
    ):
        self.storage = storage
        self.api_base = (api_base or self.BASE_URL).rstrip("/")
        self.gamma_api_base = (gamma_api_base or self.GAMMA_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._logger = logging.getLogger(__name__)
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": "Mozilla/5.0 (compatible; EsportsMonitor/1.0)"}
        )

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def get_token_id(self, condition_id: str, outcome: str) -> Optional[str]:
        """获取 token_id（带缓存）。

        Args:
            condition_id: Polymarket conditionId
            outcome: "Yes" 或 "No"

        Returns:
            token_id 字符串；失败返回 None。
        """
        if not condition_id or not outcome:
            return None
        outcome_normalized = outcome.strip().capitalize()
        if outcome_normalized not in ("Yes", "No"):
            self._logger.warning("无效的 outcome: %s，应为 Yes 或 No", outcome)
            return None

        # 1. 读缓存
        cached = self._get_cached_token_id(condition_id, outcome_normalized)
        if cached:
            return cached

        # 2. 调 API 获取
        token_id = self._fetch_token_id_from_api(condition_id, outcome_normalized)
        if token_id:
            # 3. 写缓存
            self._cache_token_id(condition_id, outcome_normalized, token_id)
        return token_id

    def get_orderbook(self, token_id: str) -> Optional[Dict[str, Any]]:
        """获取盘口 bids/asks。

        Args:
            token_id: CLOB token ID

        Returns:
            盘口字典 {"bids": [...], "asks": [...]}；失败返回 None。
        """
        if not token_id:
            return None
        url = f"{self.api_base}/book"
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.get(
                    url, params={"token_id": token_id}, timeout=self.timeout
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        return data
                    return None
                if resp.status_code == 404:
                    # 404 是终止性错误，不重试
                    self._logger.warning(
                        "[CLOB] 盘口不存在(404): token_id=%s", token_id
                    )
                    return None
                self._logger.warning(
                    "[CLOB] 获取盘口失败: token_id=%s, status=%s, attempt=%s",
                    token_id,
                    resp.status_code,
                    attempt + 1,
                )
            except requests.exceptions.Timeout:
                self._logger.warning(
                    "获取盘口超时: token_id=%s, attempt=%s", token_id, attempt + 1
                )
            except requests.exceptions.RequestException as exc:
                self._logger.warning(
                    "获取盘口异常: token_id=%s, error=%s, attempt=%s",
                    token_id,
                    exc,
                    attempt + 1,
                )
            except (ValueError, json.JSONDecodeError) as exc:
                self._logger.warning(
                    "盘口 JSON 解析失败: token_id=%s, error=%s", token_id, exc
                )
                return None
        return None

    def get_token_id_and_orderbook(
        self, condition_id: str, outcome: str
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """组合调用：获取 token_id 和盘口；404 时清缓存重试一次。

        Returns:
            (token_id, orderbook)；任一失败返回 (None, None) 或 (token_id, None)。
        """
        token_id = self.get_token_id(condition_id, outcome)
        if not token_id:
            return None, None

        orderbook = self.get_orderbook(token_id)
        if orderbook is not None:
            return token_id, orderbook

        # 盘口获取失败，可能是缓存的 token_id 过期（404）
        self._invalidate_cached_token_id(condition_id, outcome)
        fresh_token_id = self._fetch_token_id_from_api(condition_id, outcome)
        if not fresh_token_id or fresh_token_id == token_id:
            # 同一 id → 不重试
            return token_id, None

        self._cache_token_id(condition_id, outcome, fresh_token_id)
        fresh_orderbook = self.get_orderbook(fresh_token_id)
        return fresh_token_id, fresh_orderbook

    def save_orderbook_snapshot(
        self,
        match_id: str,
        team: str,
        token_id: str,
        orderbook: Dict[str, Any],
        recorded_at: str,
    ) -> bool:
        """保存盘口快照到 DB（通过 storage 层）。

        Args:
            match_id: 比赛主键
            team: team_a / team_b
            token_id: CLOB token ID
            orderbook: 盘口字典
            recorded_at: UTC ISO 时间字符串

        Returns:
            成功返回 True，失败返回 False。
        """
        if not self.storage:
            return False
        try:
            summary = parse_orderbook_summary(orderbook)
            return self.storage.insert_orderbook_snapshot(
                match_id=match_id,
                team=team,
                token_id=token_id,
                order_book_json=json.dumps(orderbook, ensure_ascii=False),
                best_bid=summary["best_bid"],
                best_ask=summary["best_ask"],
                bid_depth=summary["total_bid_depth"],
                ask_depth=summary["total_ask_depth"],
                spread=summary["spread"],
                recorded_at=recorded_at,
            )
        except Exception as exc:
            self._logger.error("保存盘口快照失败: %s", exc)
            return False

    # ------------------------------------------------------------------
    # token_id 缓存（依赖 storage 层）
    # ------------------------------------------------------------------

    def _get_cached_token_id(self, condition_id: str, outcome: str) -> Optional[str]:
        if not self.storage:
            return None
        try:
            return self.storage.get_cached_token_id(condition_id, outcome)
        except Exception as exc:
            self._logger.debug("读取 token 缓存失败: %s", exc)
            return None

    def _cache_token_id(
        self, condition_id: str, outcome: str, token_id: str
    ) -> None:
        if not self.storage:
            return
        try:
            self.storage.set_cached_token_id(condition_id, outcome, token_id)
        except Exception as exc:
            self._logger.debug("写入 token 缓存失败: %s", exc)

    def _invalidate_cached_token_id(self, condition_id: str, outcome: str) -> None:
        if not self.storage:
            return
        try:
            self.storage.invalidate_cached_token_id(condition_id, outcome)
        except Exception as exc:
            self._logger.debug("清除 token 缓存失败: %s", exc)

    def _fetch_token_id_from_api(
        self, condition_id: str, outcome: str
    ) -> Optional[str]:
        """从 Gamma /markets 端点获取 token_id。"""
        url = f"{self.gamma_api_base}/markets"
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.get(
                    url,
                    params={"condition_ids": condition_id},
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    self._logger.warning(
                        "Gamma /markets 失败: cid=%s status=%s attempt=%s",
                        condition_id,
                        resp.status_code,
                        attempt + 1,
                    )
                    continue
                data = resp.json()
                if not isinstance(data, list) or not data:
                    return None
                market = data[0]
                return self._parse_token_id(market, outcome)
            except requests.exceptions.RequestException as exc:
                self._logger.warning(
                    "Gamma /markets 请求异常: %s attempt=%s", exc, attempt + 1
                )
            except (ValueError, json.JSONDecodeError) as exc:
                self._logger.warning("Gamma /markets JSON 解析失败: %s", exc)
                return None
        return None

    @staticmethod
    def _parse_token_id(market: Dict[str, Any], outcome: str) -> Optional[str]:
        """从市场对象中按 outcome 名称匹配 token_id。

        兜底：若 outcomes 解析失败，按 Yes→index 0, No→index 1 返回。
        """
        # clobTokenIds 可能是 JSON 字符串
        clob_token_ids = market.get("clobTokenIds", [])
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except (ValueError, json.JSONDecodeError):
                return None
        if not isinstance(clob_token_ids, list) or len(clob_token_ids) < 2:
            return None

        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except (ValueError, json.JSONDecodeError):
                outcomes = []
        if isinstance(outcomes, list) and len(outcomes) == len(clob_token_ids):
            for i, oid in enumerate(outcomes):
                if str(oid).strip().capitalize() == outcome.capitalize():
                    return str(clob_token_ids[i])

        # 兜底
        if outcome.capitalize() == "Yes":
            return str(clob_token_ids[0])
        if outcome.capitalize() == "No":
            return str(clob_token_ids[1])
        return None


# ----------------------------------------------------------------------
# 盘口解析工具函数（参照 polymarket_twitter_monitor/convert/depth.py）
# ----------------------------------------------------------------------


def parse_bids(orderbook: Dict[str, Any]) -> List[Tuple[float, float]]:
    """解析买盘并降序排序（买一在前）。

    Returns:
        [(price, size), ...] 降序；空盘返回 []。
    """
    raw_bids = orderbook.get("bids", []) if isinstance(orderbook, dict) else []
    bids: List[Tuple[float, float]] = []
    if not isinstance(raw_bids, list):
        return bids
    for bid in raw_bids:
        if not isinstance(bid, dict):
            continue
        try:
            price = float(bid.get("price", 0))
            size = float(bid.get("size", 0))
            if price > 0 and size > 0:
                bids.append((price, size))
        except (ValueError, TypeError):
            continue
    bids.sort(key=lambda x: -x[0])
    return bids


def parse_asks(orderbook: Dict[str, Any]) -> List[Tuple[float, float]]:
    """解析卖盘并升序排序（卖一在前）。"""
    raw_asks = orderbook.get("asks", []) if isinstance(orderbook, dict) else []
    asks: List[Tuple[float, float]] = []
    if not isinstance(raw_asks, list):
        return asks
    for ask in raw_asks:
        if not isinstance(ask, dict):
            continue
        try:
            price = float(ask.get("price", 0))
            size = float(ask.get("size", 0))
            if price > 0 and size > 0:
                asks.append((price, size))
        except (ValueError, TypeError):
            continue
    asks.sort(key=lambda x: x[0])
    return asks


def parse_orderbook_summary(orderbook: Dict[str, Any]) -> Dict[str, float]:
    """解析盘口摘要：best_bid/ask、深度、价差、中间价。

    Returns:
        {
            "best_bid": float, "best_bid_size": float,
            "best_ask": float, "best_ask_size": float,
            "spread": float, "mid_price": float,
            "total_bid_depth": float, "total_ask_depth": float,
            "bid_levels": int, "ask_levels": int,
        }
    """
    bids = parse_bids(orderbook)
    asks = parse_asks(orderbook)
    best_bid = bids[0][0] if bids else 0.0
    best_bid_size = bids[0][1] if bids else 0.0
    best_ask = asks[0][0] if asks else 0.0
    best_ask_size = asks[0][1] if asks else 0.0
    spread = (best_ask - best_bid) if (best_bid > 0 and best_ask > 0) else 0.0
    mid_price = (
        (best_bid + best_ask) / 2.0
        if (best_bid > 0 and best_ask > 0)
        else best_bid
    )
    total_bid_depth = sum(size for _, size in bids)
    total_ask_depth = sum(size for _, size in asks)
    return {
        "best_bid": best_bid,
        "best_bid_size": best_bid_size,
        "best_ask": best_ask,
        "best_ask_size": best_ask_size,
        "spread": spread,
        "mid_price": mid_price,
        "total_bid_depth": total_bid_depth,
        "total_ask_depth": total_ask_depth,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
    }


def simulate_walk_bids(
    bids: List[Tuple[float, float]], target_size: float
) -> Tuple[float, float, bool, float]:
    """模拟市价卖出走穿买盘（bids 降序）。

    Args:
        bids: [(price, size), ...] 降序（买一在前）
        target_size: 目标卖出量

    Returns:
        (vwap, filled_size, fully_filled, slippage_ratio)
        slippage_ratio = vwap / best_bid - 1（负数表示滑点损失）
    """
    if target_size <= 0 or not bids:
        return 0.0, 0.0, False, 0.0

    remaining = target_size
    filled_size = 0.0
    total_cost = 0.0
    for price, size in bids:
        if remaining <= 0:
            break
        fill = min(size, remaining)
        total_cost += price * fill
        filled_size += fill
        remaining -= fill

    vwap = total_cost / filled_size if filled_size > 0 else 0.0
    fully_filled = filled_size >= target_size
    best_bid = bids[0][0] if bids else 0.0
    slippage_ratio = (vwap / best_bid - 1.0) if best_bid > 0 else 0.0
    return vwap, filled_size, fully_filled, slippage_ratio


def simulate_walk_asks(
    asks: List[Tuple[float, float]], target_size: float
) -> Tuple[float, float, bool, float]:
    """模拟市价买入走穿卖盘（asks 升序）。

    Args:
        asks: [(price, size), ...] 升序（卖一在前）
        target_size: 目标买入量

    Returns:
        (vwap, filled_size, fully_filled, slippage_ratio)
        slippage_ratio = vwap / best_ask - 1（正数表示滑点损失）
    """
    if target_size <= 0 or not asks:
        return 0.0, 0.0, False, 0.0

    remaining = target_size
    filled_size = 0.0
    total_cost = 0.0
    for price, size in asks:
        if remaining <= 0:
            break
        fill = min(size, remaining)
        total_cost += price * fill
        filled_size += fill
        remaining -= fill

    vwap = total_cost / filled_size if filled_size > 0 else 0.0
    fully_filled = filled_size >= target_size
    best_ask = asks[0][0] if asks else 0.0
    slippage_ratio = (vwap / best_ask - 1.0) if best_ask > 0 else 0.0
    return vwap, filled_size, fully_filled, slippage_ratio
