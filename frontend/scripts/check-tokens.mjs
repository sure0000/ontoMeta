#!/usr/bin/env node
/**
 * 检查 var(--om-*) 用的 token 是否真的定义过。
 *
 * 存在的理由是一次真实事故：审核工作台整页写的是 `--om-color-bg-container` /
 * `--om-color-border` 这类**根本不存在**的名字（从另一个同样写错的组件抄来的）。
 * CSS 对未定义变量的处理是**静默丢弃整条声明**——于是所有面板没有背景、没有边框，
 * 页面看起来"没有样式"，而构建、类型检查、lint 全部通过。
 *
 * 这类错误只有机器扫得出来，人眼看到的只是"怎么有点丑"。
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = fileURLToPath(new URL("../src", import.meta.url));
const USE = /var\(\s*(--om-[a-z0-9-]+)/gi;
const DEF = /^\s*(--om-[a-z0-9-]+)\s*:/gim;

function walk(dir) {
  return readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry);
    return statSync(full).isDirectory() ? walk(full) : [full];
  });
}

const files = walk(SRC).filter((f) => /\.(css|tsx?|jsx?)$/.test(f));

// 定义只认 .css（tokens.css 及各页样式表里的 :root / 局部定义都算）
const defined = new Set();
for (const file of files.filter((f) => f.endsWith(".css"))) {
  const text = readFileSync(file, "utf8");
  for (const match of text.matchAll(DEF)) defined.add(match[1]);
}

const problems = [];
for (const file of files) {
  const text = readFileSync(file, "utf8");
  text.split("\n").forEach((line, index) => {
    for (const match of line.matchAll(USE)) {
      if (!defined.has(match[1])) {
        problems.push(`${relative(SRC, file)}:${index + 1}  ${match[1]}`);
      }
    }
  });
}

if (problems.length > 0) {
  console.error(`发现 ${problems.length} 处使用了未定义的设计 token（声明会被静默丢弃）：\n`);
  for (const problem of problems) console.error("  " + problem);
  console.error("\n可用的 token 见 src/styles/tokens.css。");
  process.exit(1);
}

console.log(`✓ token 检查通过（${defined.size} 个已定义）`);
