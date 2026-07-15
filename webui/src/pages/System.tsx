import { useEffect, useState } from 'react';
import { Card, Row, Col, Descriptions, Statistic, Table, Tag, Spin } from 'antd';
import { useTranslation } from 'react-i18next';
import { fetchSystemStatus } from '../api';

function System() {
  const { t } = useTranslation();
  const [data, setData] = useState<any>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchSystemStatus()
      .then(setData)
      .finally(() => setLoading(false));
  }, []);

  const db = data.database || {};
  const cfg = data.config || {};
  const taskRuns = data.task_runs || {};
  const tableCounts = db.table_counts || {};

  const taskColumns = [
    { title: 'Task', dataIndex: 'name', key: 'name' },
    { title: 'Last Run', dataIndex: 'last_run', key: 'last_run', render: (v: string) => v ? new Date(v).toLocaleString() : '-' },
  ];
  const taskData = Object.entries(taskRuns).map(([name, last_run]) => ({ key: name, name, last_run }));

  const tableCountData = Object.entries(tableCounts).map(([table, count]) => ({ key: table, table, count }));
  const tableCountColumns = [
    { title: 'Table', dataIndex: 'table', key: 'table' },
    { title: t('system.records'), dataIndex: 'count', key: 'count' },
  ];

  return (
    <Spin spinning={loading}>
      <h2>{t('system.title')}</h2>

      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col span={8}>
          <Card>
            <Statistic title={t('system.db_size')} value={db.size_mb || 0} suffix="MB" />
          </Card>
        </Col>
        <Col span={8}>
          <Card>
            <Statistic title={t('system.interval')} value={cfg.polling_interval_seconds || 30} suffix={t('system.seconds')} />
          </Card>
        </Col>
        <Col span={8}>
          <Card>
            <Statistic title={t('system.idle_interval')} value={cfg.idle_interval_seconds || 300} suffix={t('system.seconds')} />
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col span={12}>
          <Card title={t('system.table_counts')} size="small">
            <Table columns={tableCountColumns} dataSource={tableCountData} size="small" pagination={false} />
          </Card>
        </Col>
        <Col span={12}>
          <Card title={t('system.task_runs')} size="small">
            <Table columns={taskColumns} dataSource={taskData} size="small" pagination={false} />
          </Card>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col span={12}>
          <Card title={t('system.morphology_config')} size="small">
            <Descriptions column={2} size="small">
              <Descriptions.Item label={t('dashboard.status')}>
                <Tag color={cfg.morphology?.enabled ? 'green' : 'red'}>
                  {cfg.morphology?.enabled ? t('system.enabled') : t('system.disabled')}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="Cooldown">{cfg.morphology?.cooldown_minutes} min</Descriptions.Item>
              <Descriptions.Item label="Resample Points">{cfg.morphology?.resample_points}</Descriptions.Item>
              <Descriptions.Item label="Window Hours">{cfg.morphology?.window_hours}h</Descriptions.Item>
              <Descriptions.Item label="Buy Price Min">{cfg.morphology?.buy_price_min}</Descriptions.Item>
              <Descriptions.Item label="Buy Price Max">{cfg.morphology?.buy_price_max}</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
        <Col span={12}>
          <Card title={t('system.simulation_config')} size="small">
            <Descriptions column={2} size="small">
              <Descriptions.Item label={t('dashboard.status')}>
                <Tag color={cfg.simulation?.enabled ? 'green' : 'red'}>
                  {cfg.simulation?.enabled ? t('system.enabled') : t('system.disabled')}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="Notional USD">${cfg.simulation?.notional_usd}</Descriptions.Item>
              <Descriptions.Item label="Use Depth">
                <Tag color={cfg.simulation?.use_depth ? 'green' : 'default'}>
                  {cfg.simulation?.use_depth ? 'Yes' : 'No'}
                </Tag>
              </Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      </Row>
    </Spin>
  );
}

export default System;
