import {
  ApartmentOutlined,
  ApiOutlined,
  AppstoreOutlined,
  FunctionOutlined,
  FolderOutlined,
  MenuFoldOutlined,
  NodeIndexOutlined,
  MenuUnfoldOutlined,
  ProfileOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { Layout, Menu, Tooltip } from "antd";
import type { MenuProps } from "antd";
import { useMemo, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { AppBreadcrumb } from "./AppBreadcrumb";
import { api } from "../api";
import { useApi } from "../hooks/useApi";
import type { DomainContext } from "../types";

const { Sider, Content } = Layout;

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
  if (pathname.startsWith("/lineage-supplement")) return "/lineage-supplement";
  if (pathname.startsWith("/business-logic")) return "/business-logic";
  if (pathname.startsWith("/tasks")) return "/tasks";
  if (pathname.startsWith("/data-apps")) return "/data-apps";
  if (pathname.startsWith("/agent-access/tools")) return "/agent-access/tools";
  if (pathname.startsWith("/agent-access/monitoring")) return "/agent-access/monitoring";
  if (pathname.startsWith("/agent-access/skills")) return "/agent-access/skills";
  if (pathname.startsWith("/agent-access/tokens")) return "/agent-access/tokens";
  if (pathname.startsWith("/agent-access")) return "/agent-access/service";
  if (pathname.startsWith("/settings")) return "/settings";
  return "/ontology";
}

function getOpenKeys(pathname: string) {
  if (pathname.startsWith("/ontology")) return ["/ontology"];
  if (pathname.startsWith("/agent-access")) return ["/agent-access"];
  return [];
}

function countLabel(count: number) {
  return (
    <span
      style={{
        marginLeft: 8,
        color: "var(--om-text-secondary)",
        fontSize: 12,
      }}
    >
      {count}
    </span>
  );
}

export function AppLayout() {
  const [collapsed, setCollapsed] = useState(true);
  const location = useLocation();
  const navigate = useNavigate();

  const { data: domains } = useApi<DomainContext[]>(async () => api.listDomains(), []);

  const selectedKey = useMemo(
    () => getSelectedKey(location.pathname, location.search),
    [location.pathname, location.search],
  );

  const defaultOpenKeys = useMemo(() => getOpenKeys(location.pathname), [location.pathname]);

  const menuItems = useMemo<MenuProps["items"]>(() => {
    const domainList = domains ?? [];

    const ontologyChildren = domainList.map((d) => ({
      key: ontologyChildKey(d.id),
      label: (
        <span>
          <span>{d.name}</span>
          {countLabel(d.published_object_type_count ?? 0)}
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
      // 排在本体建模之后：补血缘是**建模的前置**——补完要重跑起草，放在下游菜单里会
      // 让人以为它是建完模之后的事。
      { key: "/lineage-supplement", icon: <NodeIndexOutlined />, label: "血缘补录" },
      {
        key: "/business-logic",
        icon: <FunctionOutlined />,
        label: "业务逻辑",
      },
      // 任务中心曾有「任务编排」这个同级子项（手工任务链）。链退场后只剩一项，
      // 父子两层就没有意义了——拍平成一个入口。
      { key: "/tasks", icon: <ProfileOutlined />, label: "我的任务" },
      { key: "/data-apps", icon: <AppstoreOutlined />, label: "数据应用" },
      {
        key: "/agent-access",
        icon: <ApiOutlined />,
        label: "Agent 接入",
        children: [
          { key: "/agent-access/service", label: "MCP 配置" },
          { key: "/agent-access/tools", label: "MCP 工具" },
          { key: "/agent-access/monitoring", label: "审计监控" },
          { key: "/agent-access/skills", label: "技能" },
          { key: "/agent-access/tokens", label: "令牌" },
        ],
      },
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
        <div className={`app-logo${collapsed ? " app-logo--collapsed" : ""}`}>
          {!collapsed && <span className="app-logo-mark">◈</span>}
          {!collapsed && (
            <div className="app-logo-text">
              <span className="app-logo-title">ontoMeta</span>
              <span className="app-logo-subtitle">企业本体建模系统</span>
            </div>
          )}
          <Tooltip title={collapsed ? "展开侧栏" : "收起侧栏"} placement="right">
            <button
              className="app-sider-toggle"
              onClick={() => setCollapsed((c) => !c)}
              aria-label="toggle sider"
            >
              {collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            </button>
          </Tooltip>
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
        <Content className="app-content">
          <AppBreadcrumb />
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
