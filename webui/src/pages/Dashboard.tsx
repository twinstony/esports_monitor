import { useEffect, useState, useCallback } from 'react';
import { Tabs, Card, Tag, Descriptions, Empty, Spin, Select, Space, Row, Col, Statistic } from 'antd';
import { useTranslation } from 'react-i18next';
import ReactECharts from 'echarts-for-react';
import { fetchMatches, fetchMatchPrices, fetchMatchOrderbook, fetchSignals, fetchTrades } from '../api';

interface Match {
  match_id: string;
  slug: string;
  game: string;
  league: string;
  team_a: string;
  team_b: string;
  start_time: string;
  end_time: string;
  status: string;
  winning_team: string;
  discovered_at: string;
}

// 计算比赛窗口（early/mid/late）
function getWindowLabel(minutesSinceStart: number): { label: string; color: string } {
  if (minutesSinceStart < 30) return { label: 'Early', color: 'blue' };
  if (minutesSinceStart < 90) return { label: 'Mid', color: 'orange' };
  return { label: 'Late', color: 'red' };
}

// 倒计时组件
function Countdown({ startTime }: { startTime: string }) {
  const [timeLeft, setTimeLeft] = useState('');
  
  useEffect(() => {
    const update = () => {
      const now = new Date().getTime();
      const start = new Date(startTime).getTime();
      const diff = start - now;
      if (diff <= 0) {
        setTimeLeft('已开始');
        return;
      }
      const hours = Math.floor(diff / (1000 * 60 * 60));
      const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
      const seconds = Math.floor((diff % (1000 * 60)) / 1000);
      setTimeLeft(`${hours}h ${minutes}m ${seconds}s`);
    };
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [startTime]);
  
  return <Tag color="cyan">{timeLeft}</Tag>;
}

// 赛中比赛卡片（带窗口和冷却状态）
function LiveMatchCard({ match }: { match: Match }) {
  const { t } = useTranslation();
  const [cooldown, setCooldown] = useState<any>(null);
  
  useEffect(() => {
    // 检查冷却状态
    fetch(`/api/matches/${match.match_id}/cooldown`)
      .then(r => r.json())
      .then(d => setCooldown(d))
      .catch(() => {});
  }, [match.match_id]);
  
  const startDt = match.start_time ? new Date(match.start_time).getTime() : 0;
  const now = new Date().getTime();
  const minutesSinceStart = startDt ? (now - startDt) / 60000 : 0;
  const windowInfo = getWindowLabel(minutesSinceStart);
  
  return (
    <Card
      size="small"
      title={
        <Space>
          <Tag color="green">LIVE</Tag>
          <Tag>{match.game}</Tag>
          <Tag color={windowInfo.color}>{windowInfo.label}</Tag>
          <span>{match.team_a} vs {match.team_b}</span>
          {cooldown?.in_cooldown && <Tag color="red">冷却中</Tag>}
        </Space>
      }
      style={{ marginBottom: 12, borderLeft: '3px solid #52c41a' }}
    >
      <Row gutter={16}>
        <Col span={8}>
          <Descriptions column={1} size="small">
            <Descriptions.Item label={t('dashboard.league')}>{match.league || '-'}</Descriptions.Item>
            <Descriptions.Item label={t('dashboard.start_time')}>
              {match.start_time ? new Date(match.start_time).toLocaleString() : '-'}
            </Descriptions.Item>
            <Descriptions.Item label="已进行">
              <Tag color="blue">{Math.round(minutesSinceStart)} 分钟</Tag>
            </Descriptions.Item>
            {cooldown?.in_cooldown && (
              <Descriptions.Item label="冷却剩余">
                <Tag color="orange">{cooldown.remaining_minutes?.toFixed(0) || 0} 分钟</Tag>
              </Descriptions.Item>
            )}
          </Descriptions>
        </Col>
        <Col span={16}>
          <Card size="small" title={t('dashboard.price_chart')} style={{ marginBottom: 8 }}>
            <PriceChart matchId={match.match_id} teamA={match.team_a} teamB={match.team_b} />
          </Card>
          <Card size="small" title={t('dashboard.orderbook')}>
            <OrderbookInfo matchId={match.match_id} teamA={match.team_a} teamB={match.team_b} />
          </Card>
        </Col>
      </Row>
    </Card>
  );
}

function PriceChart({ matchId, teamA, teamB, showSignals = false }: { matchId: string; teamA?: string; teamB?: string; showSignals?: boolean }) {
  const [data, setData] = useState<{ team_a: any[]; team_b: any[] }>({ team_a: [], team_b: [] });
  const [signals, setSignals] = useState<any[]>([]);

  useEffect(() => {
    fetchMatchPrices(matchId).then(d => setData({ team_a: d.team_a || [], team_b: d.team_b || [] })).catch(() => {});
    if (showSignals) {
      fetchSignals({ match_id: matchId }).then(d => setSignals(d.signals || [])).catch(() => {});
    }
  }, [matchId, showSignals]);

  if (!data.team_a.length && !data.team_b.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />;

  const labelA = teamA || 'Team A';
  const labelB = teamB || 'Team B';
  const lastPriceA = data.team_a.length ? data.team_a[data.team_a.length - 1].price : null;
  const lastPriceB = data.team_b.length ? data.team_b[data.team_b.length - 1].price : null;

  const option = {
    tooltip: { trigger: 'axis' },
    legend: { data: [labelA, labelB] },
    xAxis: { type: 'category', data: data.team_a.map((p: any) => p.recorded_at?.slice(11, 16) || '') },
    yAxis: { type: 'value', min: 0, max: 1 },
    series: [
      { name: labelA, type: 'line', data: data.team_a.map((p: any) => p.price), smooth: true, showSymbol: false, itemStyle: { color: '#5B8FF9' } },
      { name: labelB, type: 'line', data: data.team_b.map((p: any) => p.price), smooth: true, showSymbol: false, itemStyle: { color: '#5AD8A6' } },
      // 信号标注点
      ...(showSignals && signals.length > 0 ? [{
        type: 'scatter',
        symbolSize: 15,
        data: signals.map((s: any) => {
          const idx = data.team_a.findIndex((p: any) => 
            p.recorded_at?.slice(0, 16) === s.detected_at?.slice(0, 16)
          );
          return [idx >= 0 ? idx : 0, s.buy_price || 0.5];
        }),
        itemStyle: { color: '#ff4d4f' },
        label: {
          show: true,
          formatter: (params: any) => {
            const sig = signals[params.dataIndex];
            return sig?.signal_name || '';
          },
          position: 'top',
        },
      }] : []),
    ],
  };
  return (
    <div>
      <ReactECharts option={option} style={{ height: 200 }} />
      {lastPriceA !== null && lastPriceB !== null && (
        <div style={{ display: 'flex', justifyContent: 'space-around', marginTop: 4 }}>
          <Tag color="blue">{labelA}: {(lastPriceA * 100).toFixed(1)}¢</Tag>
          <Tag color="green">{labelB}: {(lastPriceB * 100).toFixed(1)}¢</Tag>
        </div>
      )}
    </div>
  );
}

function OrderbookInfo({ matchId, teamA, teamB }: { matchId: string; teamA?: string; teamB?: string }) {
  const { t } = useTranslation();
  const [ob, setOb] = useState<any>({});

  useEffect(() => {
    fetchMatchOrderbook(matchId).then(setOb).catch(() => {});
  }, [matchId]);

  if (!ob.team_a && !ob.team_b) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />;

  const labelA = teamA || 'Team A';
  const labelB = teamB || 'Team B';

  return (
    <Row gutter={16}>
      {['team_a', 'team_b'].map(team => {
        const d = ob[team];
        if (!d) return null;
        return (
          <Col span={12} key={team}>
            <Card size="small" title={team === 'team_a' ? labelA : labelB}>
              <Descriptions column={2} size="small">
                <Descriptions.Item label={t('dashboard.best_bid')}>{d.best_bid?.toFixed(4) ?? '-'}</Descriptions.Item>
                <Descriptions.Item label={t('dashboard.best_ask')}>{d.best_ask?.toFixed(4) ?? '-'}</Descriptions.Item>
                <Descriptions.Item label={t('dashboard.spread')}>{d.spread?.toFixed(4) ?? '-'}</Descriptions.Item>
                <Descriptions.Item label={t('dashboard.depth')}>{d.bid_depth?.toFixed(0) ?? '-'}</Descriptions.Item>
              </Descriptions>
            </Card>
          </Col>
        );
      })}
    </Row>
  );
}

function MatchCard({ match }: { match: Match }) {
  const { t } = useTranslation();
  const statusColors: Record<string, string> = {
    discovered: 'blue', live: 'green', ended: 'orange', settled: 'default',
  };

  // 赛后信号/交易记录组件
  function MatchSignalsTrades({ matchId }: { matchId: string }) {
    const [signals, setSignals] = useState<any[]>([]);
    const [trades, setTrades] = useState<any[]>([]);
    
    useEffect(() => {
      fetchSignals({ match_id: matchId }).then(d => setSignals(d.signals || [])).catch(() => {});
      fetchTrades({}).then(d => {
        const matchTrades = (d.trades || []).filter((t: any) => t.match_id === matchId);
        setTrades(matchTrades);
      }).catch(() => {});
    }, [matchId]);
    
    if (!signals.length && !trades.length) return null;
    
    return (
      <Card size="small" title="信号与交易记录" style={{ marginTop: 8 }}>
        {signals.length > 0 && (
          <div style={{ marginBottom: 8 }}>
            <strong>信号 ({signals.length}):</strong>
            {signals.map((s, i) => (
              <Tag key={i} color="blue" style={{ margin: '2px 4px' }}>{s.signal_name} ({s.window_label})</Tag>
            ))}
          </div>
        )}
        {trades.length > 0 && (
          <div>
            <strong>交易 ({trades.length}):</strong>
            {trades.map((t, i) => (
              <Tag key={i} color={t.settled ? (t.pnl_usd > 0 ? 'success' : 'error') : 'processing'} style={{ margin: '2px 4px' }}>
                {t.buy_team} @ {t.buy_price?.toFixed(3)} | PnL: ${t.pnl_usd?.toFixed(2) || '-'}
              </Tag>
            ))}
          </div>
        )}
      </Card>
    );
  }
  
  // 赛前：显示倒计时
  if (match.status === 'discovered') {
    return (
      <Card
        size="small"
        title={
          <Space>
            <Tag color={statusColors[match.status]}>PRE-MATCH</Tag>
            <Tag>{match.game}</Tag>
            <span>{match.team_a} vs {match.team_b}</span>
            {match.start_time && <Countdown startTime={match.start_time} />}
          </Space>
        }
        style={{ marginBottom: 12 }}
      >
        <Row gutter={16}>
          <Col span={8}>
            <Descriptions column={1} size="small">
              <Descriptions.Item label={t('dashboard.league')}>{match.league || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('dashboard.start_time')}>{match.start_time ? new Date(match.start_time).toLocaleString() : '-'}</Descriptions.Item>
            </Descriptions>
          </Col>
          <Col span={16}>
            <Card size="small" title={t('dashboard.price_chart')} style={{ marginBottom: 8 }}>
              <PriceChart matchId={match.match_id} teamA={match.team_a} teamB={match.team_b} />
            </Card>
            <Card size="small" title={t('dashboard.orderbook')}>
              <OrderbookInfo matchId={match.match_id} teamA={match.team_a} teamB={match.team_b} />
            </Card>
          </Col>
        </Row>
      </Card>
    );
  }

  // 赛后：显示信号标注
  if (match.status === 'ended' || match.status === 'settled') {
    return (
      <Card
        size="small"
        title={
          <Space>
            <Tag color={statusColors[match.status]}>{match.status.toUpperCase()}</Tag>
            <Tag>{match.game}</Tag>
            <span>{match.team_a} vs {match.team_b}</span>
            {match.winning_team && <Tag color="gold">{match.winning_team} 胜</Tag>}
          </Space>
        }
        style={{ marginBottom: 12 }}
      >
        <Row gutter={16}>
          <Col span={8}>
            <Descriptions column={1} size="small">
              <Descriptions.Item label={t('dashboard.league')}>{match.league || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('dashboard.start_time')}>{match.start_time ? new Date(match.start_time).toLocaleString() : '-'}</Descriptions.Item>
              {match.winning_team && (
                <Descriptions.Item label={t('dashboard.winner')}>
                  <Tag color="gold">{match.winning_team}</Tag>
                </Descriptions.Item>
              )}
            </Descriptions>
          </Col>
          <Col span={16}>
            <Card size="small" title={t('dashboard.price_chart') + ' (含信号标注)'} style={{ marginBottom: 8 }}>
              <PriceChart matchId={match.match_id} teamA={match.team_a} teamB={match.team_b} showSignals={true} />
            </Card>
            <Card size="small" title={t('dashboard.orderbook')}>
              <OrderbookInfo matchId={match.match_id} teamA={match.team_a} teamB={match.team_b} />
            </Card>
            <MatchSignalsTrades matchId={match.match_id} />
          </Col>
        </Row>
      </Card>
    );
  }

  // 默认卡片
  return (
    <Card
      size="small"
      title={
        <Space>
          <Tag color={statusColors[match.status] || 'default'}>{match.status}</Tag>
          <Tag>{match.game}</Tag>
          <span>{match.team_a} vs {match.team_b}</span>
        </Space>
      }
      style={{ marginBottom: 12 }}
    >
      <Row gutter={16}>
        <Col span={8}>
          <Descriptions column={1} size="small">
            <Descriptions.Item label={t('dashboard.league')}>{match.league || '-'}</Descriptions.Item>
            <Descriptions.Item label={t('dashboard.start_time')}>{match.start_time ? new Date(match.start_time).toLocaleString() : '-'}</Descriptions.Item>
            {match.winning_team && (
              <Descriptions.Item label={t('dashboard.winner')}>
                <Tag color="gold">{match.winning_team}</Tag>
              </Descriptions.Item>
            )}
          </Descriptions>
        </Col>
        <Col span={16}>
          <Card size="small" title={t('dashboard.price_chart')} style={{ marginBottom: 8 }}>
            <PriceChart matchId={match.match_id} teamA={match.team_a} teamB={match.team_b} />
          </Card>
          <Card size="small" title={t('dashboard.orderbook')}>
            <OrderbookInfo matchId={match.match_id} teamA={match.team_a} teamB={match.team_b} />
          </Card>
        </Col>
      </Row>
    </Card>
  );
}

function Dashboard() {
  const { t } = useTranslation();
  const [matches, setMatches] = useState<Match[]>([]);
  const [loading, setLoading] = useState(true);
  const [gameFilter, setGameFilter] = useState<string>('');

  const load = useCallback(() => {
    setLoading(true);
    fetchMatches({ days: 30, ...(gameFilter ? { game: gameFilter } : {}) })
      .then(d => setMatches(d.matches || []))
      .finally(() => setLoading(false));
  }, [gameFilter]);

  useEffect(() => { load(); }, [load]);

  // WebSocket 实时更新
  useEffect(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsHost = import.meta.env.VITE_API_BASE_URL
      ? import.meta.env.VITE_API_BASE_URL.replace(/^https?:\/\//, '')
      : window.location.host;
    const ws = new WebSocket(`${protocol}//${wsHost}/ws`);
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'match_update') load();
      } catch {}
    };
    return () => ws.close();
  }, [load]);

  const preMatch = matches.filter(m => m.status === 'discovered');
  const live = matches.filter(m => m.status === 'live');
  const postMatch = matches.filter(m => m.status === 'ended' || m.status === 'settled');

  const gameOptions = [...new Set(matches.map(m => m.game))].filter(Boolean);

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>{t('dashboard.title')}</h2>
        <Select
          value={gameFilter}
          onChange={setGameFilter}
          style={{ width: 120 }}
          options={[
            { value: '', label: t('dashboard.all_games') },
            ...gameOptions.map(g => ({ value: g, label: g.toUpperCase() })),
          ]}
        />
      </Space>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={8}><Card><Statistic title={t('dashboard.pre_match')} value={preMatch.length} /></Card></Col>
        <Col span={8}><Card><Statistic title={t('dashboard.live')} value={live.length} valueStyle={{ color: '#52c41a' }} /></Card></Col>
        <Col span={8}><Card><Statistic title={t('dashboard.post_match')} value={postMatch.length} /></Card></Col>
      </Row>

      <Spin spinning={loading}>
        <Tabs
          defaultActiveKey="live"
          items={[
            {
              key: 'live',
              label: `${t('dashboard.live')} (${live.length})`,
              children: live.length ? live.map(m => <LiveMatchCard key={m.match_id} match={m} />) : <Empty />,
            },
            {
              key: 'pre',
              label: `${t('dashboard.pre_match')} (${preMatch.length})`,
              children: preMatch.length ? preMatch.map(m => <MatchCard key={m.match_id} match={m} />) : <Empty />,
            },
            {
              key: 'post',
              label: `${t('dashboard.post_match')} (${postMatch.length})`,
              children: postMatch.length ? postMatch.map(m => <MatchCard key={m.match_id} match={m} />) : <Empty />,
            },
          ]}
        />
      </Spin>
    </div>
  );
}

export default Dashboard;
