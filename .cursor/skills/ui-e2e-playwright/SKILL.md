---
name: ui-e2e-playwright
description: 为本项目提供前端 UI 端到端（E2E）自动化测试工作流（Playwright Node）。当用户提到“前端自动化测试 / UI 测试 / E2E / Playwright / 回归页面 / 测试页面按钮 / 冒烟测试”时使用。默认假设用户已手动启动后端（FastAPI）并在 http://127.0.0.1:8000 提供页面。
---

# UI E2E（Playwright Node）

## 目标

- 验证前端页面在真实后端下可打开、基础交互可用、关键接口调用不报错。

## 前置约定

- 用户会先手动启动后端服务（同时提供前端静态页面）。
- 默认访问地址为 `http://127.0.0.1:8000`，也可通过环境变量 `E2E_BASE_URL` 覆盖。
- 测试代码目录：`e2e/`
- Playwright 配置：`playwright.config.ts`
- 产物目录默认写到仓库内的 `.pw-test-results/` 与 `.pw-report/`，可通过环境变量覆盖：
  - `PLAYWRIGHT_OUTPUT_DIR`
  - `PLAYWRIGHT_REPORT_DIR`

## 一键运行（本机）

1. 启动后端（用户执行）：

```bash
py -m uvicorn server:app --host 127.0.0.1 --port 8000
```

2. 首次安装依赖与浏览器（只需一次）：

```bash
npm install
npx playwright install
```

3. 跑 UI 测试：

```bash
npm run test:ui
```

常用变体：

```bash
npm run test:ui:headed
npm run test:ui:debug
npm run test:ui:report
```

## 编写/扩展用例的原则（针对本项目）

- 优先写“**不依赖 LLM**”的用例，避免 flaky：
  - ✅ `GET /projects`（刷新列表）
  - ✅ 打开项目（若已有项目）
  - ❌ 「生成概要」（会触发 LLM，除非用 route stub）
  - ❌ “生成大纲 / 续写 / 重写 / 重新生成”（会触发 LLM、耗时且依赖环境）
- 断言尽量贴近用户可见结果：
  - `#global-status` 状态栏文案
  - 关键按钮是否可见/可点
  - 列表是否渲染
- 如必须覆盖 LLM 流程：
  - 通过 `E2E_BASE_URL` 指向一个可用的后端（已配置好模型/KEY/BASE_URL）。
  - 放宽超时并减少断言数量（只验证流程能走完，不追求输出内容稳定）。

## 新增测试的操作步骤（Agent 工作流）

当用户要求“新增一个 UI 用例/覆盖某个页面流程”时：

1. 先阅读 `frontend/index.html` 与 `frontend/app.js`，确认控件 id 与交互逻辑。
2. 在 `e2e/` 新建或扩展 `*.spec.ts`：
   - 使用稳定选择器：优先 `getByRole` / `locator("#id")`。
   - 对网络慢流程设置合理等待：使用 `expect(...).toBeVisible()`、`toContainText()` 而不是 `waitForTimeout`。
3. 本地执行 `npm run test:ui`，失败则根据报错：
   - 先排除“服务未启动/端口不对”（提示用户检查 `E2E_BASE_URL` 或后端启动命令）。
   - 再修复选择器、等待条件、或用例依赖前置状态问题。

## 失败排查速查表

- 报 `net::ERR_CONNECTION_REFUSED` / “无法连接后端”：
  - 后端没启动或端口不对；确认 `py -m uvicorn server:app --host 127.0.0.1 --port 8000`
  - 或设置 `E2E_BASE_URL` 指向正确地址
- 报找不到元素：
  - 先用 `page.pause()`（配合 `npm run test:ui:debug`）确认 DOM 是否渲染
  - 检查是否被 `hidden`、是否需要先打开项目/章节才显示
- 报超时：
  - 优先改用更精确的等待条件（等待状态栏文本变化、等待列表项出现）
  - 避免硬等

