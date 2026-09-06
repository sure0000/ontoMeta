import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

/**
 * 前端测试配置。
 *
 * 与 vite.config.ts 分开：那份带 dev server 的 proxy 配置，测试里既用不上、
 * 出错时又会让人以为测试在打真后端。
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // jsdom 里 antd 的动画与 rc-* 的 ResizeObserver 会刷大量噪声，
    // 真失败信息容易被埋掉；setup.ts 里已按需 stub。
    restoreMocks: true,
  },
});
