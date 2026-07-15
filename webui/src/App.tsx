import { Routes, Route, useNavigate, useLocation } from 'react-router-dom';
import { Layout, Menu, Select, Space } from 'antd';
import {
  DashboardOutlined,
  SwapOutlined,
  ApartmentOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import Dashboard from './pages/Dashboard';
import Trades from './pages/Trades';
import Morphology from './pages/Morphology';
import System from './pages/System';

const { Header, Content, Sider } = Layout;

function App() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();

  const menuItems = [
    { key: '/', icon: <DashboardOutlined />, label: t('nav.dashboard') },
    { key: '/trades', icon: <SwapOutlined />, label: t('nav.trades') },
    { key: '/morphology', icon: <ApartmentOutlined />, label: t('nav.morphology') },
    { key: '/system', icon: <SettingOutlined />, label: t('nav.system') },
  ];

  const changeLanguage = (lng: string) => {
    i18n.changeLanguage(lng);
    localStorage.setItem('language', lng);
  };

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="dark" breakpoint="lg" collapsedWidth="80">
        <div style={{ height: 48, margin: 16, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <span style={{ color: '#fff', fontSize: 18, fontWeight: 'bold', whiteSpace: 'nowrap' }}>
            Esports Monitor
          </span>
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Layout>
        <Header style={{ background: '#fff', padding: '0 24px', display: 'flex', justifyContent: 'flex-end', alignItems: 'center', borderBottom: '1px solid #f0f0f0' }}>
          <Space>
            <span>{t('common.language')}:</span>
            <Select
              value={i18n.language}
              onChange={changeLanguage}
              style={{ width: 100 }}
              options={[
                { value: 'zh', label: t('common.chinese') },
                { value: 'en', label: t('common.english') },
              ]}
            />
          </Space>
        </Header>
        <Content style={{ margin: 16, padding: 24, background: '#fff', borderRadius: 8, minHeight: 280 }}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/trades" element={<Trades />} />
            <Route path="/morphology" element={<Morphology />} />
            <Route path="/system" element={<System />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  );
}

export default App;
