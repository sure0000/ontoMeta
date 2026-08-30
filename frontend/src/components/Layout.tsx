import {
  ApartmentOutlined,
  AppstoreOutlined,
  FunctionOutlined,
  FolderOutlined,
  HistoryOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  ProfileOutlined,
  RobotOutlined,
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
  if (pathname.startsWith("/business-logic")) return "/business-logic";
  if (pathname.startsWith("/chat-bi")) return "/chat-bi";
  if (pathname.startsWith("/decisions")) return "/decisions";
  if (pathname.startsWith("/tasks/orchestration")) {
    return "/tasks/orchestration";
  }
  if (pathname.startsWith("/tasks")) return "/tasks";
  if (pathname.startsWith("/data-apps")) return "/data-apps";
  if (pathname.startsWith("/settings")) return "/settings";
  return "/ontology";
}

function getOpenKeys(pathname: string) {
  if (pathname.startsWith("/ontology")) return ["/ontology"];
  if (pathname.startsWith("/tasks")) return ["/tasks"];
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
  const [collapsed, setCollapsed] = useState(
    () => typeof window !== "undefined" && window.innerWidth <= 768,
  );
  const location = useLocation();
  const navigate = useNavigate();

  const { data: domains } = useApi<DomainContext[]>(async () => api.listDomains(), []);

  const selectedKey = useMemo(
    () => getSelectedKey(location.pathname, location.search),
    [location.pathname, location.search],
  );

  const defaultOpenKeys = useMemo(() => getOpenKeys(location.pathname), []);

  // Data Agent 等全高度三栏应用：内容区满幅铺满，去掉内边距
  const isFlushPage = location.pathname.startsWith("/chat-bi");

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
      {
        key: "/business-logic",
        icon: <FunctionOutlined />,
        label: "业务逻辑",
      },
      { key: "/chat-bi", icon: <RobotOutlined />, label: "Data Agent" },
      // 与 Data Agent 平级而不是做成它的子项：决策留痕是**跨会话**看的，
      // 塞进对话页的子菜单会把主入口从一次点击变成两次，换来的分组并不成立。
      { key: "/decisions", icon: <HistoryOutlined />, label: "决策追踪" },
      {
        key: "/tasks",
        icon: <ProfileOutlined />,
        label: "任务中心",
        children: [
          { key: "/tasks/list", label: "📋 我的任务" },
          { key: "/tasks/orchestration", label: "🔧 任务编排" },
        ],
      },
      { key: "/data-apps", icon: <AppstoreOutlined />, label: "数据应用" },
      { key: "/settings", icon: <SettingOutlined />, label: "设置" },
    ];
  }, [domains]);

  const handleMenuClick: MenuProps["onClick"] = ({ key }) => {
    if (key === "/ontology-empty") return;
    if (key === "/tasks/list") {
      navigate("/tasks");
      return;
    }
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
        <Content className={`app-content${isFlushPage ? " app-content--flush" : ""}`}>
          <AppBreadcrumb />
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
