import { useEffect, useState, useCallback } from 'react';
import { Card, Table, Tabs, Tag, Row, Col, Statistic, Select, Space, Spin, Descriptions, Empty } from 'antd';
import { useTranslation } from 'react-i18next';
import ReactECharts from 'echarts-for-react';
import { fetchTrades, fetchTradeStats, fetchTradeStatsGrouped, fetchMatchPrices, fetchTrade } from '../api';

function formatPrice(v?: number) {
  return typeof v === 'number' ? v.toFixed(4) : '-';
}

function nearestIndex(points: any[], iso?: string) {
  if (!iso || !points.length) return -1;
  const target = new Date(iso).getTime();
  let best = -1;
  let bestDelta = Number.POSITIVE_INFINITY;
  points.forEach((p, i) => {
    const ts = new Date(p.recorded_at).getTime();
    const delta = Math.abs(ts - target);
    if (Number.isFinite(delta) && delta < bestDelta) {
      best = i;
      bestDelta = delta;
    }
  });
  return best;
}

function getOpponentTeam(trade: any) {
  if (trade.buy_team === trade.team_a) return trade.team_b;
  if (trade.buy_team === trade.team_b) return trade.team_a;
  return trade.buy_team === 'team_a' ? trade.team_b : trade.team_a;
}

function TradeEvidence({ row }: { row: any }) {
  const [detail, setDetail] = useState<any>(null);
  const [prices, setPrices] = useState<{ team_a: any[]; team_b: any[] }>({ team_a: [], team_b: [] });
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      fetchTrade(row.id).catch(() => ({})),
      fetchMatchPrices(row.match_id).catch(() => ({ team_a: [], team_b: [] })),
    ]).then(([tradeData, priceData]) => {
      setDetail(tradeData);
      setPrices({ team_a: priceData.team_a || [], team_b: priceData.team_b || [] });
    }).finally(() => setLoading(false));
  }, [row.id, row.match_id]);

  const trade = detail?.trade || row;
  const signal = detail?.signal || {};
  const labels = prices.team_a.map((p: any) => p.recorded_at?.slice(5, 16) || '');
  const idx = nearestIndex(prices.team_a.length ? prices.team_a : prices.team_b, trade.opened_at);
  const teamA = trade.team_a || 'Team A';
  const teamB = trade.team_b || 'Team B';
  const buyIsA = trade.buy_team === teamA || trade.buy_team === 'team_a';
  const buySeries = buyIsA ? prices.team_a : prices.team_b;
  const opponentSeries = buyIsA ? prices.team_b : prices.team_a;
  const buyLabel = trade.buy_team || '买入队伍';
  const opponentLabel = getOpponentTeam(trade) || '对手队伍';

  const priceOption = {
    tooltip: { trigger: 'axis' },
    legend: { data: [teamA, teamB, '开单点'] },
    xAxis: { type: 'category', data: labels },
    yAxis: { type: 'value', min: 0, max: 1 },
    series: [
      { name: teamA, type: 'line', smooth: true, showSymbol: false, data: prices.team_a.map((p: any) => p.price), itemStyle: { color: '#1677ff' } },
      { name: teamB, type: 'line', smooth: true, showSymbol: false, data: prices.team_b.map((p: any) => p.price), itemStyle: { color: '#52c41a' } },
      {
        name: '开单点',
        type: 'scatter',
        symbolSize: 14,
        data: idx >= 0 ? [[idx, trade.buy_price]] : [],
        itemStyle: { color: '#fa541c' },
      },
    ],
  };

  const patternOption = {
    tooltip: { trigger: 'axis' },
    legend: { data: [buyLabel, opponentLabel] },
    xAxis: { type: 'category', data: labels },
    yAxis: { type: 'value', min: 0, max: 1 },
    series: [
      { name: buyLabel, type: 'line', smooth: true, showSymbol: false, data: buySeries.map((p: any) => p.price), itemStyle: { color: '#fa541c' } },
      { name: opponentLabel, type: 'line', smooth: true, showSymbol: false, data: opponentSeries.map((p: any) => p.price), itemStyle: { color: '#722ed1' } },
    ],
  };

  const featureNames = ['current', 'mean_all', 'mean_12h', 'mean_24h', 'ret_6h', 'ret_12h', 'ret_24h', 'ret_total'];
  const leader = signal.morph_features?.leader || {};
  const threat = signal.morph_features?.threat || {};
  const featureOption = {
    tooltip: {},
    radar: {
      indicator: featureNames.map(name => ({ name, max: name.startsWith('ret_') ? 1 : 1 })),
    },
    series: [{
      type: 'radar',
      data: [
        { name: '领先侧', value: featureNames.map(name => Math.max(0, Math.min(1, Math.abs(Number(leader[name]) || 0)))) },
        { name: '对手侧', value: featureNames.map(name => Math.max(0, Math.min(1, Math.abs(Number(threat[name]) || 0)))) },
      ],
    }],
    legend: { data: ['领先侧', '对手侧'] },
  };

  return (
    <Spin spinning={loading}>
      <Row gutter={[16, 16]}>
        <Col span={8}>
          <Card size="small" title="开单依据">
            <Descriptions column={1} size="small">
              <Descriptions.Item label="比赛">{teamA} vs {teamB}</Descriptions.Item>
              <Descriptions.Item label="信号">{signal.signal_label || trade.signal_name || '-'}</Descriptions.Item>
              <Descriptions.Item label="窗口">{trade.window_label || '-'}</Descriptions.Item>
              <Descriptions.Item label="买入">{buyLabel} @ {formatPrice(trade.buy_price)}</Descriptions.Item>
              <Descriptions.Item label="开单时间">{trade.opened_at ? new Date(trade.opened_at).toLocaleString() : '-'}</Descriptions.Item>
              <Descriptions.Item label="预测胜率">{typeof signal.predicted_win_prob === 'number' ? `${(signal.predicted_win_prob * 100).toFixed(1)}%` : '-'}</Descriptions.Item>
              <Descriptions.Item label="预测PnL">{typeof signal.predicted_pnl === 'number' ? `$${signal.predicted_pnl.toFixed(2)}` : '-'}</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small" title="开单时价格走势">
            {prices.team_a.length || prices.team_b.length ? <ReactECharts option={priceOption} style={{ height: 260 }} /> : <Empty />}
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small" title="匹配形态走势">
            {buySeries.length || opponentSeries.length ? <ReactECharts option={patternOption} style={{ height: 260 }} /> : <Empty />}
          </Card>
        </Col>
        <Col span={24}>
          <Card size="small" title="形态特征对比">
            {Object.keys(leader).length || Object.keys(threat).length ? <ReactECharts option={featureOption} style={{ height: 260 }} /> : <Empty />}
          </Card>
        </Col>
      </Row>
    </Spin>
  );
}

function Trades() {
  const { t } = useTranslation();
  const [trades, setTrades] = useState<any[]>([]);
  const [stats, setStats] = useState<any>({});
  const [grouped, setGrouped] = useState<any[]>([]);
  const [groupBy, setGroupBy] = useState('signal_name');
  const [loading, setLoading] = useState(true);
  const [filterGame, setFilterGame] = useState('');
  const [filterSignal, setFilterSignal] = useState('');
  const [filterSettled, setFilterSettled] = useState<number | undefined>(undefined);

  const load = useCallback(() => {
    setLoading(true);
    const params: any = { days: 30 };
    if (filterGame) params.game = filterGame;
    if (filterSignal) params.signal = filterSignal;
    if (filterSettled !== undefined) params.settled = filterSettled;
    Promise.all([
      fetchTrades(params),
      fetchTradeStats(30),
      fetchTradeStatsGrouped(groupBy, 30),
    ]).then(([t, s, g]) => {
      setTrades(t.trades || []);
      setStats(s);
      setGrouped(g.groups || []);
    }).finally(() => setLoading(false));
  }, [groupBy, filterGame, filterSignal, filterSettled]);

  useEffect(() => { load(); }, [load]);

  const gameOptions = [...new Set(trades.map(t => t.game).filter(Boolean))];
  const signalOptions = [...new Set(trades.map(t => t.signal_name).filter(Boolean))];

  const columns = [
    { title: t('trades.id'), dataIndex: 'id', key: 'id', width: 50, sorter: (a: any, b: any) => a.id - b.id },
    { title: t('trades.match'), key: 'match', width: 180, render: (_: any, r: any) => `${r.team_a || '?'} vs ${r.team_b || '?'}` },
    { title: t('dashboard.game'), dataIndex: 'game', key: 'game', width: 70, filters: gameOptions.map(g => ({ text: g, value: g })), onFilter: (v: any, r: any) => r.game === v },
    { title: t('trades.signal'), dataIndex: 'signal_name', key: 'signal', width: 130, filters: signalOptions.map(s => ({ text: s, value: s })), onFilter: (v: any, r: any) => r.signal_name === v },
    { title: t('trades.window'), dataIndex: 'window_label', key: 'window', width: 70 },
    { title: t('trades.buy_team'), dataIndex: 'buy_team', key: 'buy_team', width: 120 },
    { title: t('trades.buy_price'), dataIndex: 'buy_price', key: 'buy_price', width: 90, sorter: (a: any, b: any) => (a.buy_price || 0) - (b.buy_price || 0), render: (v: number) => formatPrice(v) },
    { title: t('trades.quantity'), dataIndex: 'quantity', key: 'quantity', width: 80, sorter: (a: any, b: any) => (a.quantity || 0) - (b.quantity || 0), render: (v: number) => v?.toFixed(2) },
    { title: t('trades.notional'), dataIndex: 'notional_usd', key: 'notional', width: 80, render: (v: number) => `$${v?.toFixed(0)}` },
    { title: t('trades.vwap'), dataIndex: 'vwap', key: 'vwap', width: 80, render: (v: number) => formatPrice(v) },
    { title: t('trades.opened_at'), dataIndex: 'opened_at', key: 'opened_at', width: 150, sorter: (a: any, b: any) => (a.opened_at || '').localeCompare(b.opened_at || ''), render: (v: string) => v ? new Date(v).toLocaleString() : '-' },
    {
      title: t('dashboard.status'), key: 'status', width: 80,
      render: (_: any, r: any) => {
        if (!r.settled) return <Tag color="processing">{t('trades.open')}</Tag>;
        return r.pnl_usd > 0 ? <Tag color="success">{t('trades.win')}</Tag> : <Tag color="error">{t('trades.loss')}</Tag>;
      },
    },
    {
      title: t('trades.pnl'), dataIndex: 'pnl_usd', key: 'pnl', width: 90,
      sorter: (a: any, b: any) => (a.pnl_usd || 0) - (b.pnl_usd || 0),
      render: (v: number) => v != null ? <span style={{ color: v > 0 ? '#52c41a' : '#ff4d4f' }}>${v?.toFixed(2)}</span> : '-',
    },
    { title: t('trades.settled_at'), dataIndex: 'settled_at', key: 'settled_at', width: 150, sorter: (a: any, b: any) => (a.settled_at || '').localeCompare(b.settled_at || ''), render: (v: string) => v ? new Date(v).toLocaleString() : '-' },
  ];

  const groupColumns = [
    { title: t('trades.group_key'), dataIndex: 'group_key', key: 'group_key' },
    { title: t('trades.count'), dataIndex: 'total', key: 'total' },
    { title: t('trades.settled'), dataIndex: 'settled', key: 'settled' },
    { title: t('trades.win_rate'), dataIndex: 'win_rate', key: 'win_rate', render: (v: number) => `${v?.toFixed(1)}%` },
    { title: t('trades.pnl_total'), dataIndex: 'total_pnl', key: 'total_pnl', render: (v: number) => <span style={{ color: v > 0 ? '#52c41a' : '#ff4d4f' }}>${v?.toFixed(2)}</span> },
  ];

  const statCards = [
    { title: t('trades.total_trades'), value: stats.total || 0 },
    { title: t('trades.unsettled'), value: (stats.total || 0) - (stats.settled || 0) },
    { title: t('trades.settled'), value: stats.settled || 0 },
    { title: t('trades.win_rate'), value: `${(stats.win_rate || 0).toFixed(1)}%` },
    { title: t('trades.total_pnl'), value: `$${(stats.total_pnl || 0).toFixed(2)}`, style: { color: (stats.total_pnl || 0) > 0 ? '#52c41a' : '#ff4d4f' } },
    { title: t('trades.avg_buy_price'), value: (stats.avg_buy_price || 0).toFixed(4) },
    { title: t('trades.avg_quantity'), value: (stats.avg_quantity || 0).toFixed(2) },
    { title: t('trades.max_win'), value: `$${(stats.max_win || 0).toFixed(2)}`, style: { color: '#52c41a' } },
    { title: t('trades.max_loss'), value: `$${(stats.max_loss || 0).toFixed(2)}`, style: { color: '#ff4d4f' } },
    { title: t('trades.profit_factor'), value: (stats.profit_factor || 0).toFixed(2) },
    { title: t('trades.expectancy'), value: `$${(stats.expectancy || 0).toFixed(2)}` },
    { title: t('trades.avg_hold_time'), value: stats.avg_hold_time ? `${(stats.avg_hold_time / 3600).toFixed(1)}h` : '-' },
  ];

  return (
    <Spin spinning={loading}>
      <h2>{t('trades.title')}</h2>

      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        {statCards.map((s, i) => (
          <Col span={4} key={i} style={{ minWidth: 140 }}>
            <Card size="small">
              <Statistic title={s.title} value={s.value} valueStyle={s.style} />
            </Card>
          </Col>
        ))}
      </Row>

      <Tabs
        defaultActiveKey="list"
        items={[
          {
            key: 'list',
            label: t('trades.trade_list'),
            children: (
              <>
                <Space style={{ marginBottom: 16 }} wrap>
                  <Select
                    value={filterGame}
                    onChange={setFilterGame}
                    style={{ width: 120 }}
                    placeholder="Game"
                    options={[
                      { value: '', label: t('dashboard.all_games') },
                      ...gameOptions.map(g => ({ value: g, label: g.toUpperCase() })),
                    ]}
                  />
                  <Select
                    value={filterSignal}
                    onChange={setFilterSignal}
                    style={{ width: 180 }}
                    placeholder="Signal"
                    options={[
                      { value: '', label: 'All Signals' },
                      ...signalOptions.map(s => ({ value: s, label: s })),
                    ]}
                  />
                  <Select
                    value={filterSettled}
                    onChange={(v) => setFilterSettled(v === undefined ? undefined : Number(v))}
                    style={{ width: 120 }}
                    placeholder="Status"
                    allowClear
                    options={[
                      { value: 0, label: t('trades.open') },
                      { value: 1, label: t('trades.win') },
                    ]}
                  />
                </Space>
                <Table
                  columns={columns}
                  dataSource={trades}
                  rowKey="id"
                  size="small"
                  scroll={{ x: 1400 }}
                  pagination={{ pageSize: 20 }}
                  expandable={{
                    expandedRowRender: (record) => <TradeEvidence row={record} />,
                    rowExpandable: (record) => Boolean(record.match_id),
                  }}
                  rowClassName={(r) => !r.settled ? 'ant-table-row-highlight' : ''}
                />
              </>
            ),
          },
          {
            key: 'grouped',
            label: t('trades.group_stats'),
            children: (
              <>
                <Space style={{ marginBottom: 16 }}>
                  <Select
                    value={groupBy}
                    onChange={setGroupBy}
                    style={{ width: 150 }}
                    options={[
                      { value: 'signal_name', label: t('trades.by_signal') },
                      { value: 'game', label: t('trades.by_game') },
                      { value: 'window_label', label: t('trades.by_window') },
                      { value: 'buy_team', label: t('trades.by_buy_team') },
                    ]}
                  />
                </Space>
                <Table columns={groupColumns} dataSource={grouped} rowKey="group_key" size="small" pagination={false} />
              </>
            ),
          },
        ]}
      />
    </Spin>
  );
}

export default Trades;
