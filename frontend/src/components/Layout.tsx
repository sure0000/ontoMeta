import {
  ApartmentOutlined,
  FunctionOutlined,
  FolderOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
} from "@ant-design/icons";
import { Avatar, Layout, Menu, Tooltip } from "antd";
import { useMemo, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { AppBreadcrumb } from "./AppBreadcrumb";

const { Sider, Content, Header } = Layout;

const menuItems = [
  { key: "/workspace", icon: <FolderOutlined />, label: "工作区" },
  { key: "/ontology", icon: <ApartmentOutlined />, label: "本体" },
  { key: "/business-logic", icon: <FunctionOutlined />, label: "业务逻辑" },
];

function getSelectedKey(pathname: string) {
  if (pathname.startsWith("/workspace")) return "/workspace";
  if (pathname.startsWith("/ontology")) return "/ontology";
  if (pathname.startsWith("/business-logic")) return "/business-logic";
  return "/workspace";
}

export function AppLayout() {
  const [collapsed, setCollapsed] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const selectedKey = useMemo(
    () => getSelectedKey(location.pathname),
    [location.pathname],
  );

  return (
    <Layout className="app-shell">
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        width={232}
        collapsedWidth={64}
        trigger={null}
        className="app-sider"
      >
        <div className="app-logo">
          <span className="app-logo-mark">◈</span>
          {!collapsed && (
            <div className="app-logo-text">
              <span className="app-logo-title">ontoMeta</span>
              <span className="app-logo-subtitle">企业本体建模系统</span>
            </div>
          )}
        </div>
        <Menu
          className="app-sider-menu"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
        <div className="app-sider-footer">
          {!collapsed ? (
            <>
              <span>v0.1.0 · 内部预览</span>
              <span className="app-sider-dot" />
            </>
          ) : (
            <span className="app-sider-dot" />
          )}
        </div>
      </Sider>

      <Layout>
        <Header className="app-header">
          <div className="app-header-left">
            <Tooltip title={collapsed ? "展开侧栏" : "收起侧栏"} placement="bottom">
              <button
                className="om-icon-btn"
                onClick={() => setCollapsed((c) => !c)}
                aria-label="toggle sider"
                style={{
                  border: "1px solid var(--om-border)",
                  background: "var(--om-surface)",
                  borderRadius: 8,
                  width: 32,
                  height: 32,
                  display: "grid",
                  placeItems: "center",
                  cursor: "pointer",
                  color: "var(--om-text-secondary)",
                }}
              >
                {collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
              </button>
            </Tooltip>
            <AppBreadcrumb />
          </div>
          <div className="app-header-right">
            <span className="app-env-badge">
              <span className="app-env-dot" />
              内部环境
            </span>
            <div className="app-user">
              <Avatar size={26} style={{ background: "#2563eb", fontSize: 12 }}>
                OM
              </Avatar>
              <span className="app-user-name">本体管理员</span>
            </div>
          </div>
        </Header>

        <Content className="app-content">
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
