import {
  ApartmentOutlined,
  FunctionOutlined,
  FolderOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  RobotOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { Avatar, Layout, Menu, Tooltip } from "antd";
import type { MenuProps } from "antd";
import { useMemo, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { AppBreadcrumb } from "./AppBreadcrumb";
import { api } from "../api";
import { useApi } from "../hooks/useApi";
import type { DomainContext } from "../types";

const { Sider, Content, Header } = Layout;

function ontologyChildKey(domainId: string) {
  return `/ontology?domain=${domainId}`;
}

function readDomainFromSearch(search: string) {
  return new URLSearchParams(search).get("domain") || undefined;
}

function getSelectedKey(pathname: string, search: string) {
  if (pathname.startsWith("/workspace")) return "/workspace";
  if (pathname.startsWith("/ontology")) {
    const domainId = readDomainFromSearch(search);
    return domainId ? ontologyChildKey(domainId) : "/ontology";
  }
  if (pathname.startsWith("/business-logic")) return "/business-logic";
  if (pathname.startsWith("/chat-bi")) return "/chat-bi";
  if (pathname.startsWith("/settings")) return "/settings";
  return "/ontology";
}

function getOpenKeys(pathname: string) {
  if (pathname.startsWith("/ontology")) return ["/ontology"];
  return [];
}

function countLabel(count: number) {
  return (
    <span
      style={{
        marginLeft: 8,
        color: "var(--om-text-secondary, #94a3b8)",
        fontSize: 12,
      }}
    >
      {count}
    </span>
  );
}

export function AppLayout() {
  const [collapsed, setCollapsed] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();

  const { data: domains } = useApi<DomainContext[]>(
    async () => api.listDomains(),
    [],
  );

  const selectedKey = useMemo(
    () => getSelectedKey(location.pathname, location.search),
    [location.pathname, location.search],
  );

  const defaultOpenKeys = useMemo(
    () => getOpenKeys(location.pathname),
    [],
  );

  const menuItems = useMemo<MenuProps["items"]>(() => {
    const domainList = domains ?? [];

    const ontologyChildren = domainList.map((d) => ({
      key: ontologyChildKey(d.id),
      label: (
        <span>
          <span>{d.name}</span>
          {countLabel(d.published_count)}
        </span>
      ),
    }));

    return [
      {
        key: "/ontology",
        icon: <ApartmentOutlined />,
        label: "本体浏览",
        children:
          ontologyChildren.length > 0
            ? ontologyChildren
            : [{ key: "/ontology-empty", label: "暂无数据域", disabled: true }],
      },
      { key: "/workspace", icon: <FolderOutlined />, label: "本体建模" },
      {
        key: "/business-logic",
        icon: <FunctionOutlined />,
        label: "业务逻辑",
      },
      { key: "/chat-bi", icon: <RobotOutlined />, label: "智能问数" },
      { key: "/settings", icon: <SettingOutlined />, label: "设置" },
    ];
  }, [domains]);

  const handleMenuClick: MenuProps["onClick"] = ({ key }) => {
    if (key === "/ontology-empty") return;
    if (key.startsWith("/ontology?")) {
      const [, query] = key.split("?");
      const params = new URLSearchParams(query);
      const domainId = params.get("domain");
      if (domainId) {
        navigate(`/ontology?domain=${domainId}`);
      }
      return;
    }
    navigate(key);
  };

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
          defaultOpenKeys={defaultOpenKeys}
          items={menuItems}
          onClick={handleMenuClick}
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
