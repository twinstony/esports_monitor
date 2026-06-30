# esports_monitor 架构设计文档

> 版本: v1.0
> 日期: 2026-06-29
> 状态: 设计完成，待实施

---

## 1. 概述与目标

### 1.1 项目定位

esports_monitor 是一个**独立的电竞市场监控与形态分析系统**，参照 `polymarket_twitter_monitor` 的分层架构设计，专注于采集 Polymarket 上 CS2、Dota2、LoL 等流动性好的电竞市场的价格与盘口深度数据，并结合两区间形态策略（6 种图形对比规律）从持久化数据中寻找获利机会。

### 1.2 核心目标

```
Polymarket 电竞市场 → 价格+盘口深度采集 → SQLite 持久化 → 两队价格形态对比 → 输出获利信号
```

1. **数据采集**：稳定获取电竞市场的实时价格（Gamma API）和盘口深度（CLOB API），按可配置间隔持久化。
2. **形态分析**：将推文市场的"王牌区 vs 准王牌区"两区间对比策略适配为电竞市场的"主队 vs 客队"两队价格对比，复用 6 种形态信号。
3. **获利机会发现**：基于形态信号输出买入建议（队伍方向、买入价、预测胜率、预测 PnL）。
4. **模拟交易验证**：对信号触发进行模拟下单与结算，跟踪策略表现。
5. **可扩展性**：架构上预留电竞实时数据源（Cito API 等）扩展接口。

### 1.3 设计原则

| 原则 | 说明 | 参照来源 |
|------|------|----------|
| 分层架构 | API 层 → 采集层 → 持久化层 → 形态分析层 → 通知层，各层单向依赖 | polymarket_twitter_monitor |
| 独立可运行 | 自带配置、依赖、入口，不依赖 polymarket_twitter_monitor 或 polymarket_trade_bot | 用户决策 |
| 容错优先 | 所有 DB/网络操作 try/except，失败仅记日志不中断主循环 | polymarket_twitter_monitor |
| 配置驱动 | 禁止硬编码业务数值，所有参数通过 config.yaml 管理 | .trae/rules/项目数值说明规范.md |
| UTC 内部时间 | 内部全 UTC，展示转北京时间，禁止 `datetime.now()` | polymarket_monitor/prediction/time_utils.py |
| 预留扩展 | 电竞数据源通过抽象接口预留，第一阶段空实现 | 用户决策 |

### 1.4 与现有项目的关系

| 项目 | 关系 | 说明 |
|------|------|------|
| `polymarket_twitter_monitor` | 架构参照 | 复用其分层架构、API 模式、持久化模式、调度模式、配置管理模式，不复用代码 |
| `polymarket_trade_bot` | 完全独立 | 不复用其 LoL 交易实现、Cito API 适配器、React Web UI |
| `esports_monitor/docs/` 现有文档 | 方法论参考 | morphology_strategy_design.md（形态策略设计）、回测报告作为信号逻辑来源 |

---

## 2. 整体架构

### 2.1 分层架构

```
┌─────────────────────────────────────────────────────────┐
│                    通知层 (notification)                  │
│              Telegram 告警 / 状态报告 / 结算通知            │
├─────────────────────────────────────────────────────────┤
│                  形态分析层 (morphology)                  │
│   6 种信号检测 / 冷却管理 / 模拟交易 / 回测统计加载          │
├─────────────────────────────────────────────────────────┤
│                    持久化层 (data)                        │
│        SQLite 多表存储 / 盘口快照 / 信号记录 / 去重         │
├─────────────────────────────────────────────────────────┤
│            采集与编排层 (core + discovery)                │
│      主循环调度 / 市场发现 / 流动性筛选 / 热重载             │
├─────────────────────────────────────────────────────────┤
│                      API 层 (api)                        │
│   Gamma API (价格)  │  CLOB API (盘口)  │  电竞数据 (预留)  │
├─────────────────────────────────────────────────────────┤
│                  配置层 (config + utils)                 │
│          分层配置合并 / UTC 时间工具 / 日志                 │
└─────────────────────────────────────────────────────────┘
```

### 2.2 模块划分

```
esports_monitor/
├── main.py                          # CLI 入口
├── config.yaml                      # 主配置（版本控制，非机密）
├── config.example.yaml              # 配置示例
├── config.local.example.yaml        # 本地机密配置示例
├── requirements.txt                 # 依赖
├── pytest.ini                       # 测试配置
├── esports_monitor/                 # 核心包
│   ├── __init__.py
│   ├── api/                         # API 客户端层
│   │   ├── __init__.py
│   │   ├── polymarket.py            # Gamma API（市场元数据+价格）
│   │   ├── clob.py                  # CLOB API（盘口深度）
│   │   └── esports_data.py          # 电竞实时数据接口（预留）
│   ├── config/                      # 配置管理
│   │   ├── __init__.py
│   │   └── manager.py               # 分层配置加载/合并/热重载
│   ├── core/                        # 监控编排
│   │   ├── __init__.py
│   │   └── monitor.py               # 主类（调度+编排）
│   ├── data/                        # 持久化层
│   │   ├── __init__.py
│   │   └── sqlite_storage.py        # SQLite 存储
│   ├── discovery/                   # 市场发现
│   │   ├── __init__.py
│   │   └── service.py               # 电竞市场自动发现+流动性筛选
│   ├── morphology/                  # 形态分析层
│   │   ├── __init__.py
│   │   ├── models.py                # 数据结构（dataclass）
│   │   ├── signals.py               # 6 种信号 + 特征计算
│   │   ├── detector.py              # 检测编排主逻辑
│   │   ├── simulator.py             # 模拟下单与结算
│   │   └── repository.py            # 形态信号/交易持久化
│   ├── notification/                # 通知层
│   │   ├── __init__.py
│   │   └── telegram.py              # Telegram 通知
│   └── utils/                       # 工具
│       ├── __init__.py
│       └── time_utils.py            # UTC 时间工具
├── tests/                           # 测试
│   ├── __init__.py
│   ├── test_signals.py
│   ├── test_storage.py
│   └── test_detector.py
├── scripts/                         # 脚本
│   ├── fetch_history.py             # 历史已结算市场批量抓取
│   └── restore_archive.py           # 归档数据恢复
├── archives/                        # 归档数据目录（gzip 压缩 JSON）
│   ├── price_snapshots_202601.json.gz
│   └── ...
└── docs/                            # 文档
    ├── architecture_design.md       # 本文档
    ├── morphology_strategy_design.md  # 形态策略设计（已有，方法论参考）
    └── morphology_guide.html        # 形态指南（已有）
```

### 2.3 数据流

```
[市场发现] ──> matches 表（比赛元数据）
                    │
                    ▼
[主循环遍历比赛] ──> [Gamma API] ──> price_snapshots 表（价格快照）
                    │
                    ├──> [CLOB API] ──> orderbook_snapshots 表（盘口快照）
                    │         │
                    │         └──> clob_token_cache 表（token 缓存）
                    │
                    └──> [形态检测器]
                              │
                              ├──> 加载 price_snapshots（48 点重采样）
                              ├──> 识别领先队/对手队
                              ├──> 按时间窗口检测 6 种信号
                              ├──> 写入 morphology_signals 表
                              ├──> 模拟下单 → morphology_trades 表
                              └──> 触发 Telegram 告警
```

---

## 3. API 层设计

### 3.1 Gamma API 客户端（polymarket.py）

**参照**: `polymarket_twitter_monitor/polymarket_monitor/api/polymarket.py`

Gamma API 是公开只读端点（`https://gamma-api.polymarket.com`），无需认证，用于获取市场元数据和滞后价格。

**核心方法**:

| 方法 | 端点 | 用途 |
|------|------|------|
| `fetch_events(tag_id)` | `/events?tag_id={tag_id}` | 按电竞 tag 获取赛事列表 |
| `fetch_event(slug)` | `/events?slug={slug}` | 获取单场赛事详情（含子市场） |
| `parse_match_markets(event)` | — | 从 event 解析出比赛胜负二元市场 |
| `_parse_team_names(question)` | — | 从市场问题文本解析队伍名 |

**电竞市场解析逻辑**:

```python
def parse_match_markets(self, event: dict) -> list[MatchMarket]:
    """从 event 解析出比赛胜负二元市场。

    电竞 event 通常包含多个子市场（系列赛胜负、地图胜负、MVP 等），
    本方法筛选出"比赛胜负"类型的二元市场。

    筛选条件：
    - outcomes 为二元（["Yes", "No"] 或两支队伍名）
    - question 包含 vs / defeat 等对阵关键词
    - outcomePrices 有两个有效价格
    """
    markets = []
    for m in event.get("markets", []):
        condition_id = m.get("conditionId")
        outcomes = m.get("outcomes", [])
        prices = m.get("outcomePrices", [])
        if not condition_id or len(outcomes) != 2 or len(prices) != 2:
            continue
        team_a, team_b = self._parse_team_names(m.get("question", ""))
        if not team_a or not team_b:
            continue
        markets.append(MatchMarket(
            condition_id=condition_id,
            clob_token_ids=m.get("clobTokenIds", []),
            team_a=team_a,
            team_b=team_b,
            price_a=float(prices[0]),
            price_b=float(prices[1]),
            end_date=m.get("endDate"),
            question=m.get("question"),
        ))
    return markets
```

**队伍名解析**:

电竞市场的 `question` 字段格式通常为 `"Will Team A defeat Team B?"` 或 `"Team A vs Team B"`，通过正则解析两队名称。

**错误处理**: 所有 `requests.get` 包裹 try/except，捕获 `RequestException`、`Timeout`、`JSONDecodeError`，返回 None。

### 3.2 CLOB API 客户端（clob.py）

**参照**: `polymarket_twitter_monitor/polymarket_monitor/api/clob.py`

CLOB API（`https://clob.polymarket.com`）是公开只读端点，用于获取实时盘口深度。

**映射链**: `condition_id → token_id → orderbook`

**核心方法**:

| 方法 | 端点 | 用途 |
|------|------|------|
| `get_token_id(condition_id, outcome)` | Gamma `/markets?condition_ids={cid}` | 获取 token_id（带缓存） |
| `get_orderbook(token_id)` | CLOB `/book?token_id={tid}` | 获取盘口 bids/asks |
| `get_token_id_and_orderbook(condition_id, outcome)` | — | 组合调用，404 时清缓存重试 |
| `save_orderbook_snapshot(...)` | — | 保存盘口快照到 DB |

**token_id 缓存**:

- 优先从 `clob_token_cache` 表读取
- 缓存未命中时调 Gamma API 获取（注意：用 Gamma 而非 CLOB 的 `/markets`，因为 CLOB 的 `/markets` 不支持 condition_id 过滤）
- 获取后写入缓存表
- 404 时自动清除缓存并重试

**重试机制**: `max_retries=2`，超时 `timeout=10s`。

**盘口数据结构**:

```python
{
    "bids": [{"price": "0.55", "size": "100"}, ...],  # 降序排列
    "asks": [{"price": "0.56", "size": "80"}, ...]    # 升序排列
}
```

> **注意**: CLOB API 返回顺序不保证，必须排序后使用（参照 polymarket_twitter_monitor depth.py 的 `parse_orderbook_summary`）。

### 3.3 电竞数据接口（esports_data.py）— 预留

**设计目的**: 为后续接入电竞实时数据源（Cito API、PandaScore 等）预留抽象接口，第一阶段为空实现。

**抽象接口**:

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from datetime import datetime


@dataclass
class MatchState:
    """电竞比赛实时状态。"""
    game: str               # cs2 / dota2 / lol
    match_id: str
    timestamp: datetime     # UTC
    score_a: int            # 队伍A得分（回合/小局）
    score_b: int            # 队伍B得分
    gold_diff: float = 0.0  # 金币差（LoL/Dota2）
    kill_diff: int = 0      # 击杀差
    progress_pct: float = 0.0  # 比赛进度百分比 0-1
    extra: dict = None      # 游戏特定字段（地图名、龙魂等）


class EsportsDataProvider(ABC):
    """电竞实时数据源抽象基类。"""

    @abstractmethod
    def is_available(self) -> bool:
        """数据源是否可用。"""

    @abstractmethod
    def get_match_state(self, match_id: str) -> Optional[MatchState]:
        """获取比赛实时状态。"""


class NullProvider(EsportsDataProvider):
    """空实现，第一阶段使用。"""

    def is_available(self) -> bool:
        return False

    def get_match_state(self, match_id: str) -> Optional[MatchState]:
        return None
```

**后续扩展方向**:

| Provider | 游戏 | 数据源 | 说明 |
|----------|------|--------|------|
| `CitoProvider` | LoL | Cito API | polymarket_trade_bot 中已有实现可参考 |
| `PandaScoreProvider` | 多游戏 | PandaScore API | 商业 API，覆盖 CS2/Dota2/LoL |
| `RiotProvider` | LoL | Riot Games API | 官方 API |

**形态分析层集成方式**: 当 `provider.is_available()` 为 True 时，形态检测器可选消费 `MatchState`，将比赛进度作为辅助特征（如金币领先作为动量确认）。第一阶段 provider 不可用，形态分析仅基于价格序列。

### 3.4 历史市场批量抓取（回测数据来源）

**设计目的**: Polymarket 官方 API **不提供历史价格时间序列的批量回填能力**。已结算市场的历史价格走势只能依赖程序运行期间实时采集积累。但 Gamma API 支持批量查询已结算市场列表和最终结算结果，可用于：
1. 校准结算判定逻辑（从 `outcomePrices` 判断哪队获胜）
2. 积累已结算市场元数据，为未来回测提供样本基础
3. 从现在开始持续采集，逐步积累电竞市场价格序列供未来回测

**Gamma API 批量查询能力**:

Gamma API `/events` 端点支持以下查询参数（polymarket_twitter_monitor 未使用，本项目新增利用）：

| 参数 | 作用 | 示例 |
|------|------|------|
| `closed` | 过滤已关闭市场 | `closed=true` 查已结算 |
| `active` | 过滤活跃市场 | `active=false` |
| `tag_slug` | 按标签过滤 | `tag_slug=esports` |
| `limit` | 分页大小（默认 500，最大 1000） | `limit=1000` |
| `offset` | 分页偏移 | `offset=1000` |
| `order` | 排序字段 | `order=endDate` |
| `ascending` | 排序方向 | `ascending=false` 倒序 |
| `end_date_min` | 结束时间下限（ISO 8601） | `end_date_min=2026-01-01T00:00:00Z` |
| `end_date_max` | 结束时间上限 | `end_date_max=2026-06-01T00:00:00Z` |

> **重要**: 以上参数需在实施阶段通过 `curl` 实测验证（如 `https://gamma-api.polymarket.com/events?closed=true&limit=10&tag_slug=esports`），确认返回结构和字段。

**核心方法**（新增到 polymarket.py）:

| 方法 | 端点 | 用途 |
|------|------|------|
| `fetch_closed_events(tag_slug, end_date_min, limit, offset)` | `/events?closed=true&tag_slug=...&limit=...&offset=...` | 分页拉取已结算电竞市场列表 |
| `parse_settlement(event)` | — | 从已结算 event 解析中奖队伍（`outcomePrices[0] >= 0.99` 为获胜方） |
| `fetch_all_closed_events(tag_slug, since_date)` | — | 循环分页直到无更多数据 |

**结算判定逻辑**（参照 polymarket_twitter_monitor 的 `calibrate_settlements.py`）:

```python
def parse_settlement(self, event: dict) -> Optional[dict]:
    """从已结算 event 解析中奖队伍。

    判定依据：子市场的 outcomePrices[0] >= 0.99 视为该队伍获胜。
    """
    for m in event.get("markets", []):
        prices = m.get("outcomePrices", [])
        if len(prices) != 2:
            continue
        yes_price = float(prices[0])
        if yes_price >= 0.99:
            team_a, team_b = self._parse_team_names(m.get("question", ""))
            winning_team = team_a if yes_price >= 0.5 else team_b
            return {
                "winning_team": winning_team,
                "closed": event.get("closed", False),
                "yes_price": yes_price,
                "source": "gamma_api",
            }
    return None
```

**历史抓取脚本**（scripts/fetch_history.py）:

独立脚本，一次性或定期运行，将已结算电竞市场批量写入 `matches` 表（status='ended', winning_team 填充）。**不写入 price_snapshots**（因为 Gamma API 不提供历史价格），仅填充元数据和结算结果。

```bash
# 拉取过去 6 个月的已结算电竞市场
python scripts/fetch_history.py --since 2026-01-01 --games cs2,dota2,lol

# 拉取全部历史
python scripts/fetch_history.py --all --games cs2,dota2,lol
```

**数据完整性说明**:

| 数据项 | 可批量回填 | 说明 |
|--------|-----------|------|
| 已结算市场列表 | 是 | Gamma API `closed=true&tag_slug=esports` 分页拉取 |
| 中奖队伍（结算结果） | 是 | 从 `outcomePrices` 判断 |
| 市场元数据（conditionId, tokenIds, endDate） | 是 | Gamma API event 字段 |
| 历史价格时间序列 | **否** | Gamma/CLOB API 均不提供，必须实时采集积累 |
| 历史盘口快照 | **否** | CLOB `/book` 仅返回当前盘口 |
| 历史成交明细 | **否** | 需扫 Polygon 链上 OrderFilled 事件，成本高，未实现 |

> **策略**: 第一阶段先批量拉取已结算市场元数据 + 结算结果（供结算逻辑校准），同时启动持续采集积累价格序列。积累 3-6 个月后，即可基于本地数据开展电竞形态回测（对应实施路径阶段三）。

---

## 4. 市场发现与流动性筛选设计

### 4.1 设计目标

**参照**: `polymarket_twitter_monitor/polymarket_monitor/discovery/`

自动发现 Polymarket 上的电竞市场，筛选出流动性好的比赛纳入监控，避免手动维护 watchlist。

### 4.2 发现流程

```
[定期触发（每 6 小时）]
    │
    ├──> 查询 Gamma API /events?tag_id={esports_tag}
    │         │
    │         └──> 过滤出 CS2 / Dota2 / LoL 赛事
    │
    ├──> 对每场赛事调 parse_match_markets 获取二元市场
    │
    ├──> 流动性筛选：
    │         ├──> CLOB 盘口买一深度 ≥ min_depth（默认 50 USDC）
    │         ├──> 价差 ≤ max_spread（默认 0.05）
    │         └──> 价格在 [0.05, 0.95] 区间（排除已决出胜负的市场）
    │
    ├──> 写入 matches 表（新增比赛）
    │
    └──> 记录 task_runs（去重，跨重启有效）
```

### 4.3 去重机制

- **发现任务去重**: 使用 `task_runs` 表，按 `task_name="market_discovery"` + UTC 日期判断当天是否已运行
- **比赛去重**: `matches` 表以 `match_id`（或 `condition_id`）为主键，INSERT OR IGNORE
- **运行时状态不写 config.yaml**: 避免污染版本控制（参照 polymarket_twitter_monitor scheduler.py 的设计原则）

### 4.4 比赛状态管理

`matches` 表的 `status` 字段管理比赛生命周期：

| 状态 | 含义 | 触发条件 |
|------|------|----------|
| `discovered` | 已发现，待监控 | 流动性筛选通过 |
| `live` | 比赛进行中 | start_time 已过且 end_time 未到 |
| `ended` | 比赛已结束 | end_time 已过或 Gamma API 显示已结算 |
| `settled` | 模拟交易已结算 | 形态交易完成 PnL 计算 |

### 4.5 配置

```yaml
discovery:
  enabled: true
  games: [cs2, dota2, lol]     # 支持的游戏
  tag_ids:                     # Polymarket 电竞 tag IDs（实施时通过 API 确认）
    cs2: ""
    dota2: ""
    lol: ""
  min_depth: 50                # 最小买一深度（USDC）
  max_spread: 0.05             # 最大买卖价差
  price_min: 0.05              # 价格下限（排除已决出胜负）
  price_max: 0.95              # 价格上限
  interval_hours: 6            # 发现间隔（小时）
  lookahead_count: 20          # 前瞻赛事数量
  timeout: 30                  # API 超时（秒）
```

> **说明**: `tag_ids` 需在实施阶段通过 Gamma API 实际查询确认。Polymarket 的 tag 体系可能通过 `/tags` 端点获取，或在 event 的 `tags` 字段中查找。

---

## 5. 持久化层设计

### 5.1 数据库选型

**SQLite**（单文件 `esports_history.db`），与 polymarket_twitter_monitor 保持一致。

**理由**:
- 无需额外数据库服务，部署简单
- 单机监控场景足够
- 生态成熟，Python 标准库内置 `sqlite3`

**连接管理**: 使用 `contextlib.contextmanager`，每次操作打开/关闭连接，`row_factory=True` 返回 Row 对象（参照 polymarket_twitter_monitor sqlite_storage.py:26-34）。

### 5.2 表设计

#### 5.2.1 比赛元数据表 `matches`

```sql
CREATE TABLE IF NOT EXISTS matches (
    match_id        TEXT PRIMARY KEY,      -- condition_id 作为主键
    slug            TEXT NOT NULL,         -- event slug
    game            TEXT NOT NULL,         -- cs2 / dota2 / lol
    league          TEXT,                  -- 联赛名（如 LCK, ESL）
    team_a          TEXT NOT NULL,         -- 队伍A名称
    team_b          TEXT NOT NULL,         -- 队伍B名称
    condition_id    TEXT NOT NULL,         -- Polymarket conditionId
    token_id_a      TEXT,                  -- 队伍A的 CLOB token_id
    token_id_b      TEXT,                  -- 队伍B的 CLOB token_id
    start_time      TEXT,                  -- 比赛预计开始时间（UTC ISO）
    end_time        TEXT,                  -- 比赛预计结束时间（UTC ISO）
    status          TEXT DEFAULT 'discovered',  -- discovered/live/ended/settled
    winning_team    TEXT,                  -- 结算后获胜队伍
    discovered_at   TEXT NOT NULL,         -- 发现时间（UTC ISO）
    updated_at      TEXT NOT NULL          -- 最后更新时间（UTC ISO）
);
```

#### 5.2.2 价格快照表 `price_snapshots`

```sql
CREATE TABLE IF NOT EXISTS price_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        TEXT NOT NULL,
    team            TEXT NOT NULL,         -- team_a / team_b
    price           REAL NOT NULL,         -- 该队伍的当前价格（0-1）
    price_opponent  REAL,                  -- 对手价格（冗余，便于查询）
    recorded_at     TEXT NOT NULL,         -- 记录时间（UTC ISO）
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE INDEX IF NOT EXISTS idx_price_snapshots_match_time
    ON price_snapshots(match_id, recorded_at);
```

#### 5.2.3 盘口快照表 `orderbook_snapshots`

```sql
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        TEXT NOT NULL,
    team            TEXT NOT NULL,         -- team_a / team_b
    token_id        TEXT NOT NULL,
    order_book_json TEXT NOT NULL,         -- 完整盘口 JSON（bids + asks）
    best_bid        REAL,                  -- 买一价（冗余，便于查询）
    best_ask        REAL,                  -- 卖一价
    bid_depth       REAL,                  -- 买方总深度
    ask_depth       REAL,                  -- 卖方总深度
    spread          REAL,                  -- 价差
    recorded_at     TEXT NOT NULL,         -- 记录时间（UTC ISO）
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_match_time
    ON orderbook_snapshots(match_id, recorded_at);
```

#### 5.2.4 token 缓存表 `clob_token_cache`

```sql
CREATE TABLE IF NOT EXISTS clob_token_cache (
    condition_id    TEXT NOT NULL,
    outcome         TEXT NOT NULL,         -- Yes / No 或 team_a / team_b
    token_id        TEXT NOT NULL,
    cached_at       TEXT NOT NULL,
    PRIMARY KEY (condition_id, outcome)
);
```

#### 5.2.5 形态信号表 `morphology_signals`

```sql
CREATE TABLE IF NOT EXISTS morphology_signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id            TEXT NOT NULL,
    signal_name         TEXT NOT NULL,     -- 6 种信号之一
    window_label        TEXT NOT NULL,     -- early / mid / late
    buy_team            TEXT NOT NULL,     -- 买入队伍
    buy_price           REAL NOT NULL,     -- 买入价
    hours_before_end    REAL,              -- 距预计结束小时数
    minutes_since_start REAL,              -- 距开始分钟数
    predicted_win_prob  REAL,              -- 预测胜率（来自回测统计）
    predicted_pnl       REAL,              -- 预测 PnL（来自回测统计）
    detected_at         TEXT NOT NULL,     -- 检测时间（UTC ISO）
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE INDEX IF NOT EXISTS idx_morphology_signals_match
    ON morphology_signals(match_id);
CREATE INDEX IF NOT EXISTS idx_morphology_signals_signal
    ON morphology_signals(signal_name);
CREATE INDEX IF NOT EXISTS idx_morphology_signals_time
    ON morphology_signals(detected_at);
```

#### 5.2.6 模拟交易表 `morphology_trades`

```sql
CREATE TABLE IF NOT EXISTS morphology_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        TEXT NOT NULL,
    signal_id       INTEGER NOT NULL,
    buy_team        TEXT NOT NULL,
    buy_price       REAL NOT NULL,
    quantity        REAL NOT NULL,         -- 份数 = notional_usd / buy_price
    notional_usd    REAL NOT NULL,         -- 投入金额
    vwap            REAL,                  -- 盘口走穿 VWAP（启用深度时）
    opened_at       TEXT NOT NULL,         -- 开仓时间（UTC ISO）
    settled         INTEGER DEFAULT 0,     -- 0=未结算, 1=已结算
    winning_team    TEXT,                  -- 结算后获胜队伍
    pnl_usd         REAL,                  -- 结算 PnL
    settled_at      TEXT,                  -- 结算时间（UTC ISO）
    FOREIGN KEY (match_id) REFERENCES matches(match_id),
    FOREIGN KEY (signal_id) REFERENCES morphology_signals(id)
);

CREATE INDEX IF NOT EXISTS idx_morphology_trades_match
    ON morphology_trades(match_id);
CREATE INDEX IF NOT EXISTS idx_morphology_trades_settled
    ON morphology_trades(settled);
```

#### 5.2.7 冷却状态表 `morphology_cooldown`

```sql
CREATE TABLE IF NOT EXISTS morphology_cooldown (
    match_id        TEXT PRIMARY KEY,
    cooldown_end    TEXT NOT NULL,         -- 冷却结束时间（UTC ISO）
    last_signal     TEXT,                  -- 最后触发的信号名
    updated_at      TEXT NOT NULL
);
```

#### 5.2.8 任务去重表 `task_runs`

```sql
CREATE TABLE IF NOT EXISTS task_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name       TEXT NOT NULL,
    run_at          TEXT NOT NULL,         -- 运行时间（UTC ISO）
    result_summary  TEXT,
    UNIQUE(task_name, run_at)
);

CREATE INDEX IF NOT EXISTS idx_task_runs_name_time
    ON task_runs(task_name, run_at DESC);
```

#### 5.2.9 全局状态表 `global_state`

```sql
CREATE TABLE IF NOT EXISTS global_state (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    updated_at      TEXT NOT NULL
);
```

### 5.3 持久化容错原则

**参照**: polymarket_twitter_monitor 的设计原则——"所有写入操作捕获异常并记录日志，不抛异常"（repository.py:9 注释）。

```python
def insert_price_snapshot(self, match_id, team, price, price_opponent, recorded_at):
    """插入价格快照。失败仅记日志，不抛异常。"""
    try:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO price_snapshots (match_id, team, price, price_opponent, recorded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (match_id, team, price, price_opponent, recorded_at)
            )
            conn.commit()
        return True
    except Exception as exc:
        self._logger.error("Failed to insert price snapshot: %s", exc)
        return False
```

所有 DB 操作遵循此模式：try/except 包裹，失败返回 False/None，绝不中断主循环。

### 5.4 数据归档与压缩

**设计目的**: 数据库不能无限增长。本设计支持将超过保留期（默认 3 个月）的数据归档压缩，减小主库体积，保持查询性能。

**与 polymarket_twitter_monitor 的差异**: 参照项目完全没有数据归档/清理/VACUUM 机制，数据库无限增长。本项目从设计之初即引入数据生命周期管理。

#### 5.4.1 数据生命周期

```
[实时采集] ──> 主库 esports_history.db（热数据，最近 N 天）
                    │
                    │ 超过保留期（默认 90 天）
                    ▼
              [归档任务] ──> 导出到压缩 JSON 文件（冷数据）
                    │
                    ├──> 从主库 DELETE 旧数据
                    ├──> VACUUM 回收磁盘空间
                    └──> 记录 task_runs 去重
```

#### 5.4.2 归档策略

| 表 | 保留期（热数据） | 归档方式 | 说明 |
|----|------------------|----------|------|
| `price_snapshots` | 90 天 | 导出 JSON 后 DELETE | 数据量大，按月分文件归档 |
| `orderbook_snapshots` | 90 天 | 导出 JSON 后 DELETE | 数据量最大（含完整盘口 JSON），按月分文件 |
| `morphology_signals` | 90 天 | 导出 JSON 后 DELETE | 信号记录，按月分文件 |
| `morphology_trades` | 90 天 | 导出 JSON 后 DELETE | 模拟交易记录，按月分文件 |
| `morphology_cooldown` | 不归档 | 直接 DELETE 过期行 | 冷却状态无保留价值 |
| `matches` | 永久保留 | 不归档 | 比赛元数据量小，永久保留供回测 |
| `clob_token_cache` | 永久保留 | 不归档 | 缓存表，体积小 |
| `task_runs` | 180 天 | 直接 DELETE | 去重记录，保留半年即可 |
| `global_state` | 永久保留 | 不归档 | 键值表，体积小 |

> 保留期可通过 `config.retention.days` 配置，默认 90 天。

#### 5.4.3 归档文件格式

归档文件存储在 `archives/` 目录，按 `{表名}_{年月}.json.gz` 命名：

```
esports_monitor/
└── archives/
    ├── price_snapshots_202601.json.gz
    ├── price_snapshots_202602.json.gz
    ├── orderbook_snapshots_202601.json.gz
    ├── morphology_signals_202601.json.gz
    └── morphology_trades_202601.json.gz
```

**文件格式**: gzip 压缩的 JSON Lines（每行一条记录），兼顾压缩率和流式读取能力：

```python
import gzip
import json

def archive_table_to_file(storage, table_name, year_month: str, output_dir: str):
    """将指定月份的数据归档到压缩 JSON 文件。

    Args:
        table_name: 表名（如 price_snapshots）
        year_month: 归档月份（如 "2026-01"）
        output_dir: 归档目录
    """
    filename = f"{output_dir}/{table_name}_{year_month.replace('-', '')}.json.gz"
    start_date = f"{year_month}-01T00:00:00"
    end_date = f"{year_month}-31T23:59:59"  # 简化处理，实际按月计算

    with gzip.open(filename, "wt", encoding="utf-8") as f:
        rows = storage.query_archive_data(table_name, start_date, end_date)
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return filename
```

**幂等设计**（参照 polymarket_twitter_monitor 的 `archive_hourly_rates`）:
- 归档前检查目标文件是否已存在，已存在则跳过（或可选覆盖模式）
- 归档成功后才执行 DELETE，保证数据不丢失
- DELETE 按 `recorded_at` 范围精确删除，避免误删

#### 5.4.4 归档任务调度

**触发方式**: 主循环定期检查，通过 `task_runs` 表去重（参照 polymarket_twitter_monitor 的市场发现调度模式）。

```python
def _check_and_run_archive(self):
    """定期归档旧数据。默认每周运行一次。"""
    task_name = "data_archive"
    last_run = self.storage.get_last_task_run(task_name)

    # 每周运行一次
    if last_run and (now_utc() - last_run).days < 7:
        return

    retention_days = self.config.get("retention.days", 90)
    cutoff_date = now_utc() - timedelta(days=retention_days)

    # 1. 按月归档过期数据到压缩文件
    months_to_archive = self._get_months_before(cutoff_date)
    for year_month in months_to_archive:
        for table in ["price_snapshots", "orderbook_snapshots",
                      "morphology_signals", "morphology_trades"]:
            self._archive_table_month(table, year_month)

    # 2. 删除已归档的旧数据
    for table in ["price_snapshots", "orderbook_snapshots",
                  "morphology_signals", "morphology_trades"]:
        self.storage.delete_before(table, cutoff_date)

    # 3. 清理冷却状态和过期 task_runs
    self.storage.delete_before("morphology_cooldown", cutoff_date)
    self.storage.delete_before("task_runs", now_utc() - timedelta(days=180))

    # 4. VACUUM 回收磁盘空间
    self.storage.vacuum()

    # 5. 记录任务运行
    self.storage.mark_task_run(task_name, f"archived before {cutoff_date.isoformat()}")
```

#### 5.4.5 VACUUM 与空间回收

**关键点**: SQLite 的 DELETE 不会自动回收磁盘空间（仅标记为可复用）。必须执行 `VACUUM` 才能真正缩小数据库文件。

```python
def vacuum(self):
    """执行 VACUUM 回收磁盘空间。

    注意: VACUUM 会锁定数据库，执行期间无法读写。
    应在低峰期执行（如凌晨），且数据库较大时耗时较长。
    """
    try:
        with self._get_connection() as conn:
            conn.execute("VACUUM")
        self._logger.info("Database VACUUM completed")
    except Exception as exc:
        self._logger.error("VACUUM failed: %s", exc)
```

**VACUUM 触发策略**:
- 归档任务执行后自动 VACUUM
- 仅在归档确实删除了数据时执行（避免无意义的 VACUUM）
- 可配置 `retention.vacuum_enabled` 关闭

**备选方案**: `PRAGMA auto_vacuum = INCREMENTAL` 可避免 VACUUM 的全库锁定，但需在建库时设置且不可逆。第一阶段使用手动 VACUUM，后续可评估迁移到 auto_vacuum。

#### 5.4.6 归档数据恢复

归档数据可通过脚本重新导入主库（用于回测分析）：

```bash
# 将某月的归档数据导入回主库
python scripts/restore_archive.py --table price_snapshots --month 2026-01

# 导入全部归档数据
python scripts/restore_archive.py --all
```

恢复脚本读取 `.json.gz` 文件，批量 INSERT 到对应表。适合回测时临时加载历史数据，回测完成后可再次归档删除。

#### 5.4.7 体积监控

主循环每轮记录数据库文件大小到 `global_state` 表，状态报告中包含当前数据库体积：

```python
def get_db_size_mb(self) -> float:
    """获取数据库文件大小（MB）。"""
    return os.path.getsize(self.db_path) / (1024 * 1024)
```

状态报告中显示：
```
数据库体积: 245.3 MB (热数据 90 天)
归档文件: 12 个, 总计 1.2 GB (archives/)
```

---

## 6. 形态分析层设计（核心）

### 6.1 形态策略映射

**参照文档**: `esports_monitor/docs/morphology_strategy_design.md`（两区间形态交易策略）

#### 推文市场 → 电竞市场映射

| 推文市场概念 | 电竞市场概念 | 说明 |
|--------------|--------------|------|
| 王牌区（ace/winner） | 领先队（leader） | 当前价格最高的队伍 |
| 准王牌区（quasi_ace） | 对手队（threat） | 当前价格较低的队伍 |
| 区间价格序列 | 队伍价格序列 | 从 price_snapshots 加载 |
| 距结算小时数 | 距预计结束分钟数 | 从 matches.end_time 计算 |
| 48h 窗口 | 可配置窗口（默认 2h） | 通过 config.morphology.window_hours |
| 48 点重采样 | 48 点重采样 | N_RESAMPLE=48 不变 |

#### 两条价格曲线

```
队伍A价格: [0.45, 0.48, 0.52, 0.55, 0.58, ...]  ← price_snapshots WHERE team='team_a'
队伍B价格: [0.55, 0.52, 0.48, 0.45, 0.42, ...]  ← price_snapshots WHERE team='team_b'
```

> **注意**: 电竞二元市场中两队价格之和应接近 1.0（队伍A胜 + 队伍B胜 = 100%），因此两条曲线呈镜像关系。这与推文市场不同（推文市场多个区间价格之和≈1，但王牌区和准王牌区只是其中两个）。形态信号仍然适用，因为信号关注的是**相对变化率**而非绝对价格。

### 6.2 价格序列重采样

**参照**: `two_zone_morphology_backtest.py` 的 `N_RESAMPLE = 48`

```python
N_RESAMPLE = 48  # 重采样点数，与推文市场保持一致

def resample_price_series(prices: list[tuple[datetime, float]],
                          window_hours: float,
                          now: datetime) -> list[float]:
    """将不等间隔的价格序列重采样为 48 个等间隔点。

    Args:
        prices: [(timestamp, price), ...] 从 price_snapshots 加载
        window_hours: 回溯窗口长度（小时），默认 2.0
        now: 当前时间（UTC）

    Returns:
        48 个等间隔价格点（线性插值）
    """
    window_start = now - timedelta(hours=window_hours)
    # 过滤窗口内数据
    # 线性插值到 48 个等间隔点
    # ...
```

**窗口长度选择理由**:

| 游戏典型时长 | 建议窗口 | 每点间隔 | 说明 |
|--------------|----------|----------|------|
| CS2 BO3 (2-4h) | 2.0h | ~2.5min | 覆盖最近 2 小时走势 |
| Dota2 BO3 (2-4h) | 2.0h | ~2.5min | 同上 |
| LoL BO3 (2-3h) | 2.0h | ~2.5min | 同上 |

> 窗口长度可通过 `config.morphology.window_hours` 配置，第一阶段使用默认值 2.0h，需基于实际数据校准。

### 6.3 特征计算

**参照**: `two_zone_morphology_backtest.py` 的 `compute_morphology_features`

对每个队伍的价格序列，在时刻 t 提取 10 个特征：

```python
@dataclass
class MorphologyFeatures:
    current: float          # 当前价格
    mean_all: float         # 截至当前的全均值
    mean_12h: float         # 最近 12 个点均值（≈ 30min）
    mean_24h: float         # 最近 24 个点均值（≈ 60min）
    ret_6h: float           # 6 点收益率（≈ 15min）
    ret_12h: float          # 12 点收益率（≈ 30min）
    ret_24h: float          # 24 点收益率（≈ 60min）
    ret_total: float        # 累计收益率
    price_6h_ago: float     # 6 点前价格
    price_12h_ago: float    # 12 点前价格
```

> **命名说明**: 特征名沿用推文市场的 `mean_12h` / `ret_6h` 等命名（h 代表"小时"），但在电竞场景中实际对应的是"点数"而非真实小时。保留命名一致性以便复用信号函数。每个点的时间间隔 = window_hours / N_RESAMPLE。

### 6.4 六种形态信号（复用）

**参照**: `two_zone_morphology_backtest.py` 第 150-201 行

信号函数签名不变：`(A_features, B_features) -> "A" | "B" | None`

#### 信号 1: current_and_recent_leader（强者恒强）

```python
def current_and_recent_leader(A, B):
    """当前价格更高，且最近 12 点均价也更高。"""
    if A.current > B.current and A.mean_12h > B.mean_12h:
        return "A"
    if B.current > A.current and B.mean_12h > A.mean_12h:
        return "B"
    return None
```

#### 信号 2: momentum_catcher（后发制人）

```python
def momentum_catcher(A, B):
    """当前落后，但最近 12 点涨幅明显大于对手，且自身为正。"""
    if (A.current < B.current
        and A.ret_12h > B.ret_12h + 0.1
        and A.ret_12h > 0.05):
        return "A"
    if (B.current < A.current
        and B.ret_12h > A.ret_12h + 0.1
        and B.ret_12h > 0.05):
        return "B"
    return None
```

#### 信号 3: breakout（突破反超）

```python
def breakout(A, B):
    """6 点前落后，当前反超，且 6 点涨幅 > 20%。"""
    if (A.price_6h_ago < B.price_6h_ago
        and A.current > B.current
        and A.ret_6h > 0.2):
        return "A"
    if (B.price_6h_ago < A.price_6h_ago
        and B.current > A.current
        and B.ret_6h > 0.2):
        return "B"
    return None
```

#### 信号 4: divergence（走势分化）

```python
def divergence(A, B):
    """一个队伍 12 点上涨 > 5%，另一个 12 点下跌 > 5%。"""
    if A.ret_12h > 0.05 and B.ret_12h < -0.05:
        return "A"
    if B.ret_12h > 0.05 and A.ret_12h < -0.05:
        return "B"
    return None
```

#### 信号 5: acceleration（加速领先）

```python
def acceleration(A, B):
    """当前领先且 6 点涨幅 > 12 点涨幅（加速），且 12 点涨幅为正。"""
    if (A.current > B.current
        and A.ret_6h > A.ret_12h
        and A.ret_12h > 0):
        return "A"
    if (B.current > A.current
        and B.ret_6h > B.ret_12h
        and B.ret_12h > 0):
        return "B"
    return None
```

> **注意**: 推文市场回测显示 `acceleration` 在 10-24h 和 10-18h 窗口大幅亏损（期望 -38% 和 -45.2%），短期加速可能是假突破。电竞市场需重新验证。

#### 信号 6: stable_spread（稳定领先）

```python
def stable_spread(A, B):
    """当前领先，且 24 点收益也领先。"""
    if A.current > B.current and A.ret_24h > B.ret_24h:
        return "A"
    if B.current > A.current and B.ret_24h > A.ret_24h:
        return "B"
    return None
```

### 6.5 时间窗口重新定义

**核心差异**: 推文市场按"距结算小时数"分窗口（1-5h / 10-18h / 10-24h / 24-48h），但电竞比赛时长仅 30min-4h，需重新定义。

**电竞时间窗口（建议初始值，需数据校准）**:

| 窗口标签 | 距开始分钟数 | 含义 | 建议检测规则（top3） | 理由 |
|----------|-------------|------|---------------------|------|
| `early` | 0-30 | 初始定价期，价格波动大 | current_and_recent_leader, stable_spread | 趋势确认型，避免噪声 |
| `mid` | 30-90 | 赛中波动期，关键事件频发 | breakout, momentum_catcher, divergence | 突破/反转类，捕捉动量变化 |
| `late` | 90+ 或距结束<30min | 结算前趋势确认期 | stable_spread, current_and_recent_leader | 趋势稳定型，确认最终方向 |

> **重要**: 以上窗口参数和规则配置为**建议初始值**，基于推文市场回测规律的迁移推断。第一阶段先采集数据，阶段三基于真实电竞数据回测后校准。

### 6.6 检测编排（detector.py）

**参照**: `morphology_strategy_design.md` 的实时检测流程

每轮对每场监控中的比赛执行：

```
1. 冷却检查
   └──> 查 morphology_cooldown 表，若在冷却期内则跳过

2. 加载价格序列
   ├──> 从 price_snapshots 加载过去 window_hours 的两队价格
   └──> 重采样为 48 点

3. 识别领先队/对手队
   └──> 当前价格高的为领先队（leader），低的为对手队（threat）

4. 计算时间窗口
   └──> 根据距开始分钟数确定当前窗口（early/mid/late）

5. 信号检测
   ├──> 对当前窗口的 top3 规则逐一检测
   ├──> A = 领先队特征, B = 对手队特征
   └──> 返回 "A"（买领先队）或 "B"（买对手队）或 None

6. 信号聚合
   └──> 同一买入方向的信号累加强度，取最强信号

7. 买入价过滤
   └──> 0.05 < buy_price < 0.95（排除无意义价位）

8. 持久化信号
   └──> 写入 morphology_signals 表

9. 模拟下单
   ├──> 若启用 simulation.enabled
   ├──> 计算买入份数 = notional_usd / buy_price
   ├──> 若启用 use_depth，用盘口 VWAP 替代当前价
   └──> 写入 morphology_trades 表

10. 设置冷却
    └──> 写入 morphology_cooldown 表

11. 触发通知
    └──> 发送 Telegram 告警
```

### 6.7 模拟交易与结算（simulator.py）

**参照**: `morphology_strategy_design.md` 的模拟交易设计

**开仓**:

```python
def open_trade(self, signal: MorphologySignal, use_depth: bool = True):
    """开模拟仓。"""
    buy_price = signal.buy_price

    # 若启用盘口深度，用 VWAP 替代当前价
    if use_depth and self.clob_client:
        vwap = self._fetch_vwap(signal.match_id, signal.buy_team, target_size)
        if vwap and 0 < vwap < 1.0:
            buy_price = vwap

    quantity = self.notional_usd / buy_price

    trade = MorphologyTrade(
        match_id=signal.match_id,
        signal_id=signal.id,
        buy_team=signal.buy_team,
        buy_price=buy_price,
        quantity=quantity,
        notional_usd=self.notional_usd,
        opened_at=now_utc(),
    )
    self.repository.save_trade(trade)
```

**结算**:

```python
def settle_trade(self, trade: MorphologyTrade, winning_team: str):
    """比赛结束后结算。"""
    if trade.buy_team == winning_team:
        # 中奖：每份结算价 1.0
        pnl_usd = trade.notional_usd * (1.0 / trade.buy_price - 1.0)
    else:
        # 未中奖：损失全部投入
        pnl_usd = -trade.notional_usd

    self.repository.update_trade_settlement(
        trade.id, winning_team=winning_team, pnl_usd=pnl_usd, settled_at=now_utc()
    )
```

**结算触发**: 主循环每轮检查 `matches` 表中 `status='ended'` 的比赛，对其未结算的交易执行结算。获胜队伍从 Gamma API 的结算数据获取。

### 6.8 回测统计加载

**参照**: `two_zone_morphology_backtest.py` 的 `load_backtest_stats`

```python
def load_backtest_stats(backtest_dir: str) -> dict:
    """加载回测统计，为实时信号提供预测胜率/PnL。

    第一阶段：加载推文市场回测统计（docs/backtest_results/）
    后续阶段：加载电竞市场回测统计（基于采集数据生成）

    返回结构:
    {
        "early": {
            "current_and_recent_leader": {
                "win_rate": 0.556,
                "avg_pnl": 0.116,
                "expectancy": 0.116,
                "profit_factor": 1.61,
                "historical_trades": 18
            },
            ...
        },
        "mid": {...},
        "late": {...}
    }
    """
```

> **跨市场迁移说明**: 第一阶段使用推文市场回测统计作为初始参考，但需明确标注"跨市场迁移可信度待验证"。推文市场与电竞市场的价格动力学不同（推文计数是单调递增的，而电竞比赛比分是双向博弈），回测统计可能不完全适用。

### 6.9 盘口深度在形态策略中的应用

**参照**: `polymarket_twitter_monitor/polymarket_monitor/convert/depth.py` 的 `DepthAnalyzer`

**盘口走穿模拟**（模拟市价买入的 VWAP）:

```python
def simulate_walk(bids: list[tuple[float, float]], target_size: float):
    """模拟市价买入走穿盘口。

    Args:
        bids: [(price, size), ...] 降序排列（买一在前）
        target_size: 目标买入量

    Returns:
        (vwap, filled_size, fully_filled, slippage_ratio)
    """
    total_cost = 0.0
    filled_size = 0.0
    remaining = target_size

    for price, size in bids:
        fill = min(size, remaining)
        total_cost += price * fill
        filled_size += fill
        remaining -= fill
        if remaining <= 0:
            break

    vwap = total_cost / filled_size if filled_size > 0 else 0
    fully_filled = filled_size >= target_size
    best_bid = bids[0][0] if bids else 0
    slippage_ratio = vwap / best_bid - 1.0 if best_bid > 0 else 0

    return vwap, filled_size, fully_filled, slippage_ratio
```

> **与 polymarket_twitter_monitor 的差异**: 推文市场的 `DepthAnalyzer` 模拟**卖出**走穿（因为 Convert 策略是卖出炮灰区），电竞形态策略模拟**买入**走穿（因为信号是买入某队）。算法逻辑相同，方向相反——卖出走穿 bids，买入走穿 asks。

---

## 7. 调度层设计

### 7.1 主循环

**参照**: `polymarket_twitter_monitor/polymarket_monitor/core/monitor.py` 的 `run_continuous`

```python
def run_continuous(self):
    """主循环。单线程 while + time.sleep。"""
    interval_seconds = self.config.get("polling.interval_seconds", 30)
    idle_interval_seconds = self.config.get("polling.idle_interval_seconds", 300)
    self._running = True

    while self._running:
        try:
            # 1. 热重载配置检查
            self._reload_config_if_changed()

            # 2. 市场发现（按间隔，task_runs 去重）
            self._check_and_run_discovery()

            # 3. 遍历监控中的比赛
            live_matches = self.storage.get_live_matches()
            for match in live_matches:
                self._check_single_match(match)

            # 4. 检查已结束比赛的结算
            self._settle_ended_matches()

            # 5. 数据归档（每周一次，task_runs 去重）
            self._check_and_run_archive()

            # 6. 发送状态报告（即使无信号也定期发送）
            self._check_and_send_status_report()

        except Exception as exc:
            self._logger.error("Error in monitoring loop: %s", exc)

        # 间隔等待（可中断）
        has_live = len(live_matches) > 0 if 'live_matches' in dir() else False
        wait = interval_seconds if has_live else idle_interval_seconds
        for _ in range(wait):
            if not self._running:
                break
            time.sleep(1)
```

### 7.2 单场比赛检查流程

```python
def _check_single_match(self, match: Match):
    """对单场比赛执行一轮检查。"""
    # 1. 获取 Gamma 价格
    event = self.gamma_api.fetch_event(match.slug)
    markets = self.gamma_api.parse_match_markets(event)
    if not markets:
        return

    # 2. 写入价格快照
    now = now_utc()
    for m in markets:
        if m.condition_id == match.match_id:
            self.storage.insert_price_snapshot(
                match.match_id, "team_a", m.price_a, m.price_b, now)
            self.storage.insert_price_snapshot(
                match.match_id, "team_b", m.price_b, m.price_a, now)

            # 3. 获取 CLOB 盘口并写入快照
            self._fetch_and_save_orderbook(match, m)

            # 4. 形态检测
            if self.morphology_detector:
                self.morphology_detector.detect(match)
            break

    # 5. 检查比赛是否结束
    if self._is_match_ended(match, event):
        self.storage.update_match_status(match.match_id, "ended")
```

### 7.3 周期任务去重

| 任务 | 去重方式 | 触发条件 |
|------|----------|----------|
| 市场发现 | task_runs 表 | 每 interval_hours（默认 6h） |
| 状态报告 | 内存时间戳 | 每 N 小时（可配置） |
| 比赛结算检查 | 无（每轮执行） | 主循环每轮 |
| 数据归档 | task_runs 表 | 每周一次（默认，可配置） |
| VACUUM | 归档任务内触发 | 归档删除数据后自动执行 |
| 配置热重载 | 文件 mtime 检查 | 主循环每轮 |

### 7.4 配置热重载

**参照**: `polymarket_twitter_monitor/monitor.py:1042-1077`

每轮检查 `config.yaml` 和 `config.local.yaml` 的 mtime，变化时重新 load 并重建所有客户端。

```python
def _reload_config_if_changed(self):
    """检查配置文件 mtime，变化时热重载。"""
    yaml_mtime = os.path.getmtime("config.yaml")
    local_mtime = os.path.getmtime("config.local.yaml") if os.path.exists("config.local.yaml") else 0

    if (yaml_mtime != self._last_yaml_mtime
        or local_mtime != self._last_local_mtime):
        self._logger.info("Config changed, reloading...")
        self.config = load_config()
        self._init_components()  # 重建所有客户端
        self._last_yaml_mtime = yaml_mtime
        self._last_local_mtime = local_mtime
```

### 7.5 轮询间隔策略

| 场景 | 间隔 | 配置项 |
|------|------|--------|
| 有比赛进行中（live） | 30s | `polling.interval_seconds` |
| 无比赛进行中 | 300s | `polling.idle_interval_seconds` |

赛中价格波动快，需要更高频率采集；非赛中无需频繁轮询。

---

## 8. 配置管理设计

### 8.1 分层配置

**参照**: `polymarket_twitter_monitor/polymarket_monitor/config/manager.py`

```
DEFAULT_CONFIG (代码内置默认值)
    ← config.yaml (主配置，版本控制，非机密)
        ← config.local.yaml (本地覆盖，机密，gitignore)
            ← 环境变量 (机密，部署用)
```

使用 `_deep_merge` 递归合并。

### 8.2 机密管理

```python
SECRET_ENV_MAP = {
    ("notification", "telegram", "bot_token"): "ESPORTS_MONITOR_TELEGRAM_BOT_TOKEN",
    ("notification", "telegram", "chat_id"): "ESPORTS_MONITOR_TELEGRAM_CHAT_ID",
}
```

`save()` 时自动将机密字段从 config.yaml 剥离，写入 config.local.yaml，保证机密不进版本控制。

### 8.3 完整 config.yaml 结构

```yaml
# Polymarket API 配置
polymarket:
  gamma_api_base: https://gamma-api.polymarket.com
  clob_api_base: https://clob.polymarket.com
  timeout: 10

# 市场发现
discovery:
  enabled: true
  games: [cs2, dota2, lol]
  tag_ids:
    cs2: ""
    dota2: ""
    lol: ""
  min_depth: 50              # 最小买一深度（USDC）
  max_spread: 0.05           # 最大买卖价差
  price_min: 0.05
  price_max: 0.95
  interval_hours: 6
  lookahead_count: 20
  timeout: 30

# 轮询配置
polling:
  interval_seconds: 30       # 赛中轮询间隔
  idle_interval_seconds: 300 # 非赛中轮询间隔

# 形态分析
morphology:
  enabled: true
  cooldown_minutes: 15       # 每场比赛冷却时间
  resample_points: 48        # 重采样点数
  window_hours: 2.0          # 价格序列回溯窗口
  buy_price_min: 0.05        # 买入价下限
  buy_price_max: 0.95        # 买入价上限
  backtest_dir: docs/backtest_results  # 回测统计目录
  simulation:
    enabled: true
    notional_usd: 100        # 每单投入金额
    use_depth: true          # 启用盘口深度感知
  windows:
    - label: early
      min_minutes: 0
      max_minutes: 30
      rules: [current_and_recent_leader, stable_spread]
    - label: mid
      min_minutes: 30
      max_minutes: 90
      rules: [breakout, momentum_catcher, divergence]
    - label: late
      min_minutes: 90
      max_minutes: 9999
      rules: [stable_spread, current_and_recent_leader]

# 电竞实时数据源（预留，第一阶段不可用）
esports_data:
  enabled: false
  provider: null             # null / cito / pandastage

# 通知
notification:
  telegram:
    enabled: true
    bot_token: ''            # 机密，写入 config.local.yaml
    chat_id: ''              # 机密
    status_report_enabled: true
    status_report_interval_hours: 6  # 状态报告间隔

# 数据保留与归档
retention:
  days: 90                   # 热数据保留天数（超过则归档）
  archive_dir: archives      # 归档文件目录
  archive_interval_days: 7   # 归档任务执行间隔（天）
  vacuum_enabled: true       # 归档后是否执行 VACUUM
  compress: true             # 归档文件是否 gzip 压缩

# 日志
logging:
  level: INFO
  file: esports_monitor.log
  max_bytes: 10485760        # 10MB
  backup_count: 5
```

### 8.4 config.example.yaml

与 config.yaml 相同但所有机密字段留空，作为新用户的配置模板。

### 8.5 config.local.example.yaml

```yaml
notification:
  telegram:
    bot_token: 'your-bot-token-here'
    chat_id: 'your-chat-id-here'
```

---

## 9. 通知层设计

### 9.1 Telegram 通知

**参照**: `polymarket_twitter_monitor/polymarket_monitor/notification/telegram.py`

**参照用户偏好**: "即使无信号也定期发送确认系统运行"（user_profile.md）

#### 通知类型

| 类型 | 触发条件 | 内容 |
|------|----------|------|
| 形态信号告警 | 信号检测命中 | 信号名称、买入队伍、买入价、时间窗口、预测胜率/PnL、比赛信息 |
| 状态报告 | 每 N 小时（可配置） | 监控中比赛数、采集数据量、最近信号数、模拟交易 PnL 汇总 |
| 模拟交易结算 | 比赛结束结算 | 比赛结果、买入队伍、是否中奖、PnL |
| 错误告警 | 连续错误超过阈值 | 错误摘要、影响范围 |

#### 信号告警格式示例

```
🎮 [形态信号] breakout — 突破反超
━━━━━━━━━━━━━━━━━━━━━
比赛: T1 vs Gen.G (LoL)
窗口: mid (45min since start)
买入: T1 @ 0.476
预测胜率: 71.4% | 预测PnL: +23.8%
盘口VWAP: 0.482 (滑点 1.3%)
━━━━━━━━━━━━━━━━━━━━━
⏰ 2026-06-29 15:30 UTC (23:30 北京时间)
```

#### 状态报告格式示例

```
📊 [esports_monitor 状态报告]
━━━━━━━━━━━━━━━━━━━━━
监控中比赛: 5 场 (3 LoL, 1 CS2, 1 Dota2)
本周期采集: 价格快照 480 条, 盘口快照 240 条
最近 6h 信号: 3 个 (breakout×2, stable_spread×1)
模拟交易: 累计 12 笔, 已结算 8 笔
  胜率: 62.5% (5胜3负)
  累计PnL: +$147.50
系统状态: 运行正常
━━━━━━━━━━━━━━━━━━━━━
```

### 9.2 预留 Webhook 接口

架构上预留 `notification/webhook.py`，第一阶段不实现，后续可扩展为 HTTP POST 推送。

---

## 10. 时间工具设计

### 10.1 UTC 时间规范

**参照**: `polymarket_twitter_monitor/polymarket_monitor/prediction/time_utils.py`

**核心原则**:
- 内部时间全 UTC
- 展示时间转北京时间（UTC+8）
- 禁止 `datetime.now()`，统一使用 `datetime.now(timezone.utc)`
- 数据库存储 ISO 8601 格式的 UTC 时间字符串

```python
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))

def now_utc() -> datetime:
    """获取当前 UTC 时间。"""
    return datetime.now(timezone.utc)

def to_utc_iso(dt: datetime) -> str:
    """转 UTC ISO 字符串。"""
    return dt.astimezone(timezone.utc).isoformat()

def from_utc_iso(s: str) -> datetime:
    """从 UTC ISO 字符串解析。"""
    return datetime.fromisoformat(s).astimezone(timezone.utc)

def to_beijing_str(dt: datetime) -> str:
    """转北京时间显示字符串。"""
    return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
```

---

## 11. 日志设计

**参照**: `polymarket_twitter_monitor/polymarket_monitor/config/manager.py:274-313`

```python
def setup_logging(config: dict):
    """配置日志。RotatingFileHandler + StreamHandler。"""
    log_config = config.get("logging", {})

    # 强制 stdout/stderr UTF-8（Windows 兼容）
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    handler = RotatingFileHandler(
        log_config.get("file", "esports_monitor.log"),
        maxBytes=log_config.get("max_bytes", 10485760),
        backupCount=log_config.get("backup_count", 5),
        encoding="utf-8",
    )
    console = StreamHandler(sys.stdout)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    console.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(log_config.get("level", "INFO"))
    root.addHandler(handler)
    root.addHandler(console)

    # 抑制高频第三方库日志
    for lib in ["httpx", "httpcore", "urllib3"]:
        logging.getLogger(lib).setLevel(logging.WARNING)
```

---

## 12. 入口与启动

### 12.1 main.py

```python
"""esports_monitor 入口。"""
import argparse
import os
import sys

def main():
    # 确保 UTF-8 控制台
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    # 将脚本目录加入 sys.path
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    os.chdir(script_dir)

    parser = argparse.ArgumentParser(description="Esports Market Monitor")
    parser.add_argument("--mode", choices=["console"], default="console")
    args = parser.parse_args()

    if args.mode == "console":
        run_console()

def run_console():
    from esports_monitor.config.manager import load_config, setup_logging
    from esports_monitor.core.monitor import Monitor

    config = load_config()
    setup_logging(config)
    monitor = Monitor()
    monitor.run_continuous()

if __name__ == "__main__":
    main()
```

### 12.2 启动命令

```bash
cd d:\workspace\python\esports_monitor
python main.py
```

---

## 13. 依赖项

### 13.1 requirements.txt

```
requests               # HTTP 客户端（Gamma/CLOB/Telegram）
pyyaml                 # 配置文件解析
numpy                  # 数值计算（重采样/特征计算）
pandas                 # 数据处理（价格序列分析）
scipy                  # 统计计算（可选，用于回测分析）
python-telegram-bot>=20.0  # Telegram 通知（v20+ asyncio）
pytest                 # 测试
pytest-cov             # 测试覆盖率
```

### 13.2 与 polymarket_twitter_monitor 依赖的差异

| 依赖 | polymarket_twitter_monitor | esports_monitor | 说明 |
|------|---------------------------|-----------------|------|
| playwright | 有 | 无 | 不需要浏览器抓取 |
| prophet | 可选 | 无 | 不需要预测模型 |
| matplotlib | 有 | 无 | 第一阶段不生成热力图 |
| psutil | 有 | 无 | 不需要系统监控 |

---

## 14. 实施路径

### 14.1 分阶段实施

| 阶段 | 目标 | 产出 | 文档覆盖 |
|------|------|------|----------|
| 阶段一 | 数据采集+持久化+归档 | API 层 + 持久化层 + 市场发现 + 调度 + 数据归档，能稳定采集价格和盘口深度并管理数据生命周期 | 本文档 §3,4,5,7,8,10,11,12 |
| 阶段二 | 形态分析 | 形态信号检测 + 模拟交易 + 通知，复用 6 种信号 | 本文档 §6,9 |
| 阶段三 | 回测校准 | 基于采集数据回测，校准时间窗口和信号参数；含历史市场批量抓取积累样本 | 本文档 §3.4, §6.5 标注的待校准项 |
| 阶段四 | 电竞数据集成 | 接入实时电竞数据，增强形态分析 | 本文档 §3.3 扩展接口 |

### 14.2 阶段一验收标准

- [ ] `python main.py` 能启动并进入主循环
- [ ] 市场发现能找到至少 1 场流动性达标的电竞比赛
- [ ] price_snapshots 表有数据写入
- [ ] orderbook_snapshots 表有数据写入
- [ ] clob_token_cache 表正确缓存 token_id
- [ ] 配置热重载生效
- [ ] 所有 DB/网络错误仅记日志不崩溃
- [ ] 数据归档任务能将超过保留期的数据导出到 archives/ 目录
- [ ] 归档后主库对应数据已删除
- [ ] VACUUM 执行后数据库文件体积减小
- [ ] 归档文件可通过 restore_archive.py 恢复

### 14.3 阶段二验收标准

- [ ] 6 种信号函数实现并通过单元测试
- [ ] 形态检测器能对监控中的比赛执行检测
- [ ] 触发信号时写入 morphology_signals 表
- [ ] 模拟交易写入 morphology_trades 表
- [ ] 比赛结束后正确结算 PnL
- [ ] Telegram 收到信号告警
- [ ] 定期收到状态报告

### 14.4 阶段三验收标准

- [ ] 基于采集数据生成电竞市场回测报告
- [ ] 校准时间窗口参数（early/mid/late 的分钟范围）
- [ ] 校准各窗口的 top3 规则配置
- [ ] 校准信号阈值参数（如 ret_6h 的 0.2 阈值）
- [ ] 更新 config.yaml 为校准后参数

---

## 15. 假设与待验证项

| 编号 | 假设/待验证项 | 影响 | 验证方式 | 阶段 |
|------|--------------|------|----------|------|
| A1 | Polymarket 电竞市场以赛事为 event，含比赛胜负二元子市场 | API 层解析逻辑 | 实施时通过 Gamma API 实际查询 | 阶段一 |
| A2 | 电竞市场的 conditionId / clobTokenIds 字段与推文市场格式一致 | CLOB API 映射链 | 实施时通过 API 验证 | 阶段一 |
| A3 | tag_id / tag_slug 可用于筛选电竞市场 | 市场发现逻辑 | 实施时查询 Gamma /tags 端点 | 阶段一 |
| A4 | 6 种信号参数（如 ret_6h > 0.2）适用于电竞市场 | 形态分析准确性 | 基于采集数据回测 | 阶段三 |
| A5 | early/mid/late 时间窗口的分钟范围合理 | 形态分析准确性 | 基于采集数据回测 | 阶段三 |
| A6 | 推文市场回测统计可跨市场迁移到电竞市场 | 预测胜率/PnL 可信度 | 基于采集数据回测对比 | 阶段三 |
| A7 | window_hours=2.0 能覆盖足够的形态信息 | 形态分析准确性 | 基于采集数据回测 | 阶段三 |
| A8 | min_depth=50 / max_spread=0.05 的流动性阈值合理 | 市场发现筛选 | 实际观察调整 | 阶段一 |
| A9 | Gamma API `closed=true&tag_slug=esports&limit=1000` 分页参数可用 | 历史市场批量抓取 | 实施时 curl 实测 | 阶段一 |
| A10 | Gamma API 已结算市场的 outcomePrices 可判定中奖队伍 | 结算逻辑 | 实施时 curl 实测 | 阶段一 |
| A11 | 持续采集 3-6 个月可积累足够回测样本 | 回测可行性 | 数据积累后评估 | 阶段三 |
| A12 | 90 天保留期满足实时分析需求，归档数据可按需恢复 | 数据生命周期 | 运行后评估 | 阶段一 |

---

## 附录 A: 推文市场回测规律参考

以下为推文市场的回测结果，作为电竞市场形态策略的初始参考。**跨市场迁移可信度待验证**。

### 6 种信号在 4 个时间窗口的表现（first_signal 模式）

| 信号 | 24-48h | 10-24h | 10-18h | 1-5h |
|------|--------|--------|--------|------|
| current_and_recent_leader | **+11.6%** ★ | +2.2% | +6.7% | -10.1% |
| momentum_catcher | +7.1% | +11.7% | +1.7% | **+2.9%** ★ |
| breakout | -17.7% | **+23.8%** ★ | -1.8% | -32.9% |
| divergence | -1.0% | +3.1% | +2.3% | -21.9% |
| acceleration | -0.6% | -38.0% | -45.2% | -11.4% |
| stable_spread | -5.5% | +1.8% | **+7.9%** ★ | +0.1% |

★ = 该窗口的最佳规则

### 核心规律

1. **远期窗口**（24-48h）：适合趋势确认型信号 `current_and_recent_leader`
2. **中期窗口**（10-24h）：适合早期突破型信号 `breakout`（表现最佳，期望 +23.8%）
3. **中短期窗口**（10-18h）：适合趋势稳定型信号 `stable_spread`
4. **临近结算窗口**（1-5h）：整体期望偏低，相对最优为 `momentum_catcher`
5. **反向规律**：`acceleration`（加速领先）在中短期窗口反而大幅亏损，短期加速可能是假突破

### 电竞市场映射建议

| 推文市场窗口 | 电竞市场窗口 | 映射理由 |
|-------------|-------------|----------|
| 24-48h（远期） | early（0-30min） | 比赛初期，趋势确认 |
| 10-24h（中期） | mid（30-90min） | 比赛中段，突破/反转 |
| 10-18h（中短期） | mid/late 交界 | 趋势稳定 |
| 1-5h（临近结算） | late（90min+） | 临近结算，趋势确认 |

> **注意**: 以上映射为推断，非严格对应关系。推文市场的"距结算小时数"与电竞市场的"距开始分钟数"语义不同，需基于实际数据校准。

---

## 附录 B: 参照来源索引

| 参照文件 | 用途 |
|----------|------|
| `polymarket_twitter_monitor/polymarket_monitor/api/polymarket.py` | Gamma API 客户端模式 |
| `polymarket_twitter_monitor/polymarket_monitor/api/clob.py` | CLOB API 客户端 + token 缓存 |
| `polymarket_twitter_monitor/polymarket_monitor/convert/depth.py` | 盘口走穿模拟算法 |
| `polymarket_twitter_monitor/polymarket_monitor/data/sqlite_storage.py` | SQLite 存储模式 + 容错原则 |
| `polymarket_twitter_monitor/polymarket_monitor/config/manager.py` | 分层配置 + 机密管理 + 日志配置 |
| `polymarket_twitter_monitor/polymarket_monitor/core/monitor.py` | 主循环调度 + 热重载 |
| `polymarket_twitter_monitor/polymarket_monitor/discovery/` | 市场发现 + task_runs 去重 |
| `polymarket_twitter_monitor/polymarket_monitor/prediction/time_utils.py` | UTC 时间规范 |
| `polymarket_twitter_monitor/scripts/two_zone_morphology_backtest.py` | 6 种信号 + 特征计算 + 回测方法论 |
| `polymarket_twitter_monitor/docs/morphology_strategy_design.md` | 形态策略实时集成设计 |
| `polymarket_twitter_monitor/docs/backtest_results/` | 回测统计数据 |
| `esports_monitor/docs/morphology_strategy_design.md` | 形态策略设计（已有，方法论参考） |
| `polymarket_twitter_monitor/.trae/rules/项目数值说明规范.md` | 禁止硬编码业务数值规则 |
