import { useEffect, useState, useMemo } from 'react';
import { Card, Row, Col, Spin, Table, Tag, Descriptions, Empty, Tabs, Select, Space } from 'antd';
import { useTranslation } from 'react-i18next';
import ReactECharts from 'echarts-for-react';
import { fetchSignals, fetchSignalTypes, fetchSignalSeries, fetchMatchPrices } from '../api';

const SIGNAL_COLORS: Record<string, string> = {
  current_and_recent_leader: '#5B8FF9',
  momentum_catcher: '#5AD8A6',
  breakout: '#F6BD16',
  divergence: '#E86452',
  acceleration: '#6DC8EC',
  stable_spread: '#945FB9',
};

function formatPrice(v?: number) {
  return typeof v === 'number' ? v.toFixed(4) : '-';
}

// 信号类型示例图：用 /signals/types 返回的 example 数据绘制形态走势
function SignalTypeChart({ type }: { type: any }) {
  const { t } = useTranslation();
  const example = type.example || { team_a: [], team_b: [] };
  const labels = example.team_a.map((_: any, i: number) => `T${i + 1}`);

  const option = {
    tooltip: { trigger: 'axis' },
    legend: { data: [t('morphology.leader'), t('morphology.threat')], top: 0 },
    grid: { top: 30, left: 40, right: 10, bottom: 20 },
    xAxis: { type: 'category', data: labels, axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', min: 0, max: 1, axisLabel: { fontSize: 10 } },
    series: [
      {
        name: t('morphology.leader'),
        type: 'line',
        smooth: true,
        showSymbol: false,
        data: example.team_a,
        itemStyle: { color: SIGNAL_COLORS[type.name] || '#5B8FF9' },
        areaStyle: { opacity: 0.08 },
      },
      {
        name: t('morphology.threat'),
        type: 'line',
        smooth: true,
        showSymbol: false,
        data: example.team_b,
        itemStyle: { color: '#bfbfbf' },
      },
    ],
  };

  return <ReactECharts option={option} style={{ height: 180 }} />;
}

function SignalTypesSection() {
  const { t } = useTranslation();
  const [types, setTypes] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchSignalTypes()
      .then(d => setTypes(d.types || []))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <Spin />;
  if (!types.length) return <Empty />;

  return (
    <Row gutter={[16, 16]}>
      {types.map(tp => (
        <Col span={8} key={tp.name}>
          <Card
            size="small"
            title={
              <Space>
                <Tag color={SIGNAL_COLORS[tp.name] || 'blue'} style={{ color: '#fff' }}>
                  {tp.label_zh || tp.label_en}
                </Tag>
                <span style={{ fontSize: 12, color: '#999' }}>{tp.name}</span>
              </Space>
            }
          >
            <SignalTypeChart type={tp} />
            <Descriptions column={1} size="small" style={{ marginTop: 8 }}>
              <Descriptions.Item label={t('morphology.description')}>
                {tp.description_zh || tp.description_en}
              </Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      ))}
    </Row>
  );
}

// 信号分布统计
function DistributionSection() {
  const { t } = useTranslation();
  const [signals, setSignals] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchSignals({ days: 30 })
      .then(d => setSignals(d.signals || []))
      .finally(() => setLoading(false));
  }, []);

  const bySignal = useMemo(() => {
    const m: Record<string, number> = {};
    signals.forEach(s => { const k = s.signal_name || 'unknown'; m[k] = (m[k] || 0) + 1; });
    return Object.entries(m).map(([name, value]) => ({ name, value }));
  }, [signals]);

  const byWindow = useMemo(() => {
    const m: Record<string, number> = {};
    signals.forEach(s => { const k = s.window_label || 'unknown'; m[k] = (m[k] || 0) + 1; });
    return Object.entries(m).map(([name, value]) => ({ name, value }));
  }, [signals]);

  const byGame = useMemo(() => {
    const m: Record<string, number> = {};
    signals.forEach(s => { const k = s.game || 'unknown'; m[k] = (m[k] || 0) + 1; });
    return Object.entries(m).map(([name, value]) => ({ name, value }));
  }, [signals]);

  if (loading) return <Spin />;
  if (!signals.length) return <Empty description={t('common.loading')} />;

  const pieOption = (data: any[], title: string) => ({
    title: { text: title, left: 'center', textStyle: { fontSize: 13 } },
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    series: [{
      type: 'pie',
      radius: ['35%', '60%'],
      data,
      label: { formatter: '{b}\n{d}%', fontSize: 10 },
    }],
  });

  return (
    <Row gutter={16}>
      <Col span={8}>
        <Card size="small">
          <ReactECharts option={pieOption(bySignal, t('morphology.signal_distribution'))} style={{ height: 260 }} />
        </Card>
      </Col>
      <Col span={8}>
        <Card size="small">
          <ReactECharts option={pieOption(byWindow, t('morphology.window_distribution'))} style={{ height: 260 }} />
        </Card>
      </Col>
      <Col span={8}>
        <Card size="small">
          <ReactECharts option={pieOption(byGame, t('morphology.game_distribution'))} style={{ height: 260 }} />
        </Card>
      </Col>
    </Row>
  );
}

// 单条信号的展开详情：实际价格走势 + 信号标注
function SignalSeriesDetail({ signal }: { signal: any }) {
  const { t } = useTranslation();
  const [series, setSeries] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    // 优先用 signal_id 拉取后端预计算序列，失败则回退到 match prices
    fetchSignalSeries(signal.id)
      .then(d => {
        if (d && (d.team_a?.length || d.team_b?.length)) {
          setSeries(d);
        } else {
          throw new Error('no series');
        }
      })
      .catch(() => fetchMatchPrices(signal.match_id).then(d => setSeries(d)))
      .catch(() => setSeries(null))
      .finally(() => setLoading(false));
  }, [signal.id, signal.match_id]);

  if (loading) return <Spin size="small" />;
  if (!series) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />;

  const teamA = series.team_a_name || signal.team_a || 'Team A';
  const teamB = series.team_b_name || signal.team_b || 'Team B';
  const pointsA = series.team_a || [];
  const pointsB = series.team_b || [];
  const labels = pointsA.map((p: any) => p.recorded_at?.slice(11, 16) || '');

  const sigIdx = (() => {
    const target = signal.detected_at ? new Date(signal.detected_at).getTime() : NaN;
    let best = -1, bestDelta = Infinity;
    pointsA.forEach((p: any, i: number) => {
      const delta = Math.abs(new Date(p.recorded_at).getTime() - target);
      if (delta < bestDelta) { best = i; bestDelta = delta; }
    });
    return best;
  })();

  const color = SIGNAL_COLORS[signal.signal_name] || '#ff4d4f';
  const option = {
    tooltip: { trigger: 'axis' },
    legend: { data: [teamA, teamB] },
    xAxis: { type: 'category', data: labels },
    yAxis: { type: 'value', min: 0, max: 1 },
    series: [
      { name: teamA, type: 'line', smooth: true, showSymbol: false, data: pointsA.map((p: any) => p.price), itemStyle: { color: '#5B8FF9' } },
      { name: teamB, type: 'line', smooth: true, showSymbol: false, data: pointsB.map((p: any) => p.price), itemStyle: { color: '#5AD8A6' } },
      ...(sigIdx >= 0 ? [{
        name: t('morphology.price_with_signal'),
        type: 'scatter',
        symbolSize: 16,
        data: [[sigIdx, signal.buy_price || 0.5]],
        itemStyle: { color },
        label: { show: true, formatter: signal.signal_label || signal.signal_name, position: 'top', color },
      }] : []),
    ],
  };

  return (
    <Row gutter={16}>
      <Col span={16}>
        <ReactECharts option={option} style={{ height: 240 }} />
      </Col>
      <Col span={8}>
        <Descriptions column={1} size="small" title={t('morphology.trade_basis')}>
          <Descriptions.Item label={t('dashboard.game')}>{signal.game || '-'}</Descriptions.Item>
          <Descriptions.Item label={t('trades.match')}>{signal.team_a || '-'} vs {signal.team_b || '-'}</Descriptions.Item>
          <Descriptions.Item label={t('trades.signal')}>
            <Tag color={color} style={{ color: '#fff' }}>{signal.signal_label || signal.signal_name}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label={t('dashboard.window')}>{signal.window_label || '-'}</Descriptions.Item>
          <Descriptions.Item label={t('trades.buy_team')}>{signal.buy_team || '-'}</Descriptions.Item>
          <Descriptions.Item label={t('trades.buy_price')}>{formatPrice(signal.buy_price)}</Descriptions.Item>
          <Descriptions.Item label={t('morphology.predicted_win_prob')}>
            {typeof signal.predicted_win_prob === 'number' ? `${(signal.predicted_win_prob * 100).toFixed(1)}%` : '-'}
          </Descriptions.Item>
          <Descriptions.Item label={t('morphology.predicted_pnl')}>
            {typeof signal.predicted_pnl === 'number' ? `${(signal.predicted_pnl * 100).toFixed(1)}%` : '-'}
          </Descriptions.Item>
        </Descriptions>
      </Col>
    </Row>
  );
}

// 信号时间线
function TimelineSection() {
  const { t } = useTranslation();
  const [signals, setSignals] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterSignal, setFilterSignal] = useState('');
  const [filterGame, setFilterGame] = useState('');

  const load = () => {
    setLoading(true);
    const params: any = { days: 30 };
    if (filterSignal) params.signal_name = filterSignal;
    fetchSignals(params)
      .then(d => {
        let list = d.signals || [];
        if (filterGame) list = list.filter((s: any) => s.game === filterGame);
        setSignals(list);
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, [filterSignal, filterGame]);

  const signalOptions = [...new Set(signals.map(s => s.signal_name).filter(Boolean))];
  const gameOptions = [...new Set(signals.map(s => s.game).filter(Boolean))];

  const columns = [
    { title: 'ID', dataIndex: 'id', key: 'id', width: 60 },
    { title: t('trades.signal'), key: 'signal', width: 130, render: (_: any, r: any) => (
      <Tag color={SIGNAL_COLORS[r.signal_name] || 'blue'} style={{ color: '#fff' }}>{r.signal_label || r.signal_name}</Tag>
    ) },
    { title: t('dashboard.game'), dataIndex: 'game', key: 'game', width: 70, render: (v: string) => v ? v.toUpperCase() : '-' },
    { title: t('trades.match'), key: 'match', width: 180, render: (_: any, r: any) => `${r.team_a || '?'} vs ${r.team_b || '?'}` },
    { title: t('dashboard.window'), dataIndex: 'window_label', key: 'window', width: 70 },
    { title: t('trades.buy_team'), dataIndex: 'buy_team', key: 'buy_team', width: 110 },
    { title: t('trades.buy_price'), dataIndex: 'buy_price', key: 'buy_price', width: 80, render: (v: number) => formatPrice(v) },
    { title: '检测时间', dataIndex: 'detected_at', key: 'detected_at', width: 150, sorter: (a: any, b: any) => (a.detected_at || '').localeCompare(b.detected_at || ''), render: (v: string) => v ? new Date(v).toLocaleString() : '-' },
  ];

  return (
    <Spin spinning={loading}>
      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          value={filterSignal}
          onChange={setFilterSignal}
          style={{ width: 180 }}
          placeholder={t('trades.signal')}
          options={[
            { value: '', label: t('dashboard.all_games') },
            ...signalOptions.map(s => ({ value: s, label: s })),
          ]}
        />
        <Select
          value={filterGame}
          onChange={setFilterGame}
          style={{ width: 120 }}
          placeholder={t('dashboard.game')}
          options={[
            { value: '', label: t('dashboard.all_games') },
            ...gameOptions.map(g => ({ value: g, label: g.toUpperCase() })),
          ]}
        />
      </Space>
      <Table
        columns={columns}
        dataSource={signals}
        rowKey="id"
        size="small"
        scroll={{ x: 1100 }}
        pagination={{ pageSize: 20 }}
        expandable={{
          expandedRowRender: (record) => <SignalSeriesDetail signal={record} />,
          rowExpandable: (record) => Boolean(record.match_id),
        }}
      />
    </Spin>
  );
}

function Morphology() {
  const { t } = useTranslation();

  return (
    <div>
      <h2>{t('morphology.title')}</h2>
      <Tabs
        defaultActiveKey="types"
        items={[
          {
            key: 'types',
            label: t('morphology.signal_types'),
            children: <SignalTypesSection />,
          },
          {
            key: 'dist',
            label: t('morphology.signal_distribution'),
            children: <DistributionSection />,
          },
          {
            key: 'timeline',
            label: t('morphology.timeline'),
            children: <TimelineSection />,
          },
        ]}
      />
    </div>
  );
}

export default Morphology;
