import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "antd/dist/reset.css";
import "./styles.css";

const FONT_FAMILY =
  '-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Microsoft YaHei", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
const CODE_FONT_FAMILY =
  '"SF Mono", "JetBrains Mono", "Fira Code", Menlo, Monaco, Consolas, "Courier New", monospace';

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#2563eb",
          colorInfo: "#2563eb",
          colorSuccess: "#16a34a",
          colorWarning: "#d97706",
          colorError: "#dc2626",
          colorTextBase: "#0f172a",
          colorBgLayout: "#f6f8fb",
          borderRadius: 10,
          borderRadiusLG: 12,
          borderRadiusSM: 8,
          fontFamily: FONT_FAMILY,
          fontFamilyCode: CODE_FONT_FAMILY,
          fontSize: 14,
          controlHeight: 34,
          controlHeightLG: 40,
          controlHeightSM: 28,
          wireframe: false,
        },
        components: {
          Layout: {
            siderBg: "#ffffff",
            headerBg: "#ffffff",
            headerHeight: 56,
            headerPadding: "0 24px",
            bodyBg: "#f6f8fb",
          },
          Menu: {
            itemBg: "transparent",
            itemColor: "#475569",
            itemHoverColor: "#0f172a",
            itemHoverBg: "#eff4ff",
            itemSelectedColor: "#2563eb",
            itemSelectedBg: "#eff4ff",
            itemBorderRadius: 8,
            itemMarginInline: 12,
            itemMarginBlock: 4,
            itemHeight: 38,
            iconSize: 16,
            subMenuItemBg: "transparent",
          },
          Card: {
            colorBgContainer: "#ffffff",
            colorBorderSecondary: "#eef1f6",
            paddingLG: 20,
            headerFontSize: 15,
            headerHeight: 48,
            boxShadowTertiary: "0 1px 2px rgba(15, 23, 42, 0.04)",
          },
          Table: {
            headerBg: "#f8fafc",
            headerColor: "#64748b",
            headerSplitColor: "transparent",
            rowHoverBg: "#f6f8fb",
            borderColor: "#eef1f6",
            cellPaddingBlock: 12,
            cellPaddingInline: 14,
          },
          Button: {
            controlHeight: 34,
            paddingInline: 16,
            fontWeight: 500,
            primaryShadow: "none",
            defaultShadow: "none",
            dangerShadow: "none",
          },
          Tag: {
            borderRadiusSM: 6,
            defaultBg: "#f1f5f9",
            defaultColor: "#475569",
          },
          Breadcrumb: {
            itemColor: "#94a3b8",
            linkColor: "#64748b",
            linkHoverColor: "#0f172a",
            lastItemColor: "#0f172a",
            fontSize: 13,
          },
          Segmented: {
            itemSelectedColor: "#0f172a",
            itemSelectedBg: "#ffffff",
            trackBg: "#eef1f6",
            trackPadding: 3,
            borderRadius: 8,
            borderRadiusSM: 6,
          },
          Descriptions: {
            labelColor: "#94a3b8",
            contentColor: "#0f172a",
          },
          Input: {
            controlHeight: 34,
            activeShadow: "0 0 0 3px rgba(37, 99, 235, 0.12)",
          },
          Select: {
            controlHeight: 34,
            optionSelectedBg: "#eff4ff",
            optionSelectedColor: "#2563eb",
          },
          Tooltip: {
            colorBgSpotlight: "#0f172a",
            borderRadius: 8,
          },
          Spin: {
            colorPrimary: "#2563eb",
          },
          Empty: {
            colorText: "#94a3b8",
            colorTextDisabled: "#cbd5e1",
          },
          Form: {
            labelColor: "#475569",
            labelFontSize: 13,
            itemMarginBottom: 16,
          },
          Divider: {
            colorSplit: "#eef1f6",
          },
        },
      }}
    >
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ConfigProvider>
  </StrictMode>,
);
