# 🚀 百万字分章小说 Agent（本地工作台）

这是一个本地“分章写小说”工作台：从创作意图生成剧情概要与结构化大纲；随后逐章续写、润色，并在最新章上支持反馈重写。写章会结合本地 RAG（检索前文摘要）与人物图谱上下文，来减少前后不一致。

详细实现与阶段验收见 `docs/项目实现学习指南.md`。

## 🧩 项目简介（30 秒看懂）

- 从创作意图生成剧情概要与结构化大纲
- 逐章续写/润色；最新章支持反馈重写
- 结合本地 RAG 与人物图谱上下文保证连续性


## 🛠️ 依赖安装与快速启动

### 1. 准备环境变量

复制示例文件：

```bash
copy .env-sample .env
```

在 `.env` 中配置：

- `MODEL`：默认模型名（OpenAI-compatible 或 Ollama-compatible，必须匹配你实际的本地/远端服务）
- `BASE_URL`：模型服务基地址（默认示例使用 Ollama 风格：`http://localhost:11434/v1`）
- `API_KEY`：模型服务的 API Key（Ollama 默认可填 `ollama`）

<details><summary>高级配置（可选）</summary>

可选覆盖（由 `config.py` 读取）：

- `PLANNER_MODEL` / `PLANNER_BASE_URL` / `PLANNER_API_KEY`：规划用模型
- `WRITER_MODEL` / `WRITER_BASE_URL` / `WRITER_API_KEY`：写作用模型
- `CHAPTER_WORD_TARGET`：每章目标字数（默认 3000）
- `DEFAULT_TOTAL_CHAPTERS`：默认目标章节数（默认 100）
- `PLOT_IDEAS_COUNT`：生成剧情概要候选数量（默认 5）
- `RAG_PREVIOUS_CHAPTERS`：写章时检索前文章节摘要数量（默认 5）
- `PLAN_OUTLINE_SINGLE_CALL_MAX`：超过该章节数则大纲改为「骨架 + 分批扩写」多轮 LLM（默认 16）
- `PLAN_OUTLINE_BATCH_SIZE`：大纲分批扩写时每批章节数（默认 10）
- `PLAN_OUTLINE_LARGE_BOOK_CHAPTERS`：达到该章节数后每章要点条数降为 2～3（默认 40）

</details>

### 2. 启动后端（FastAPI）

安装 Python 依赖：

```bash
pip install -r requirements.txt
```

运行服务：

```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```

启动后打开：

- `http://127.0.0.1:8000/`（后端会直接挂载并提供前端静态资源）

你不需要单独“启动/构建前端”。只要后端已运行，就可以直接在浏览器中使用 UI：

- 新建项目：输入创作意图 → 点击 `生成概要`
- 选概要并生成大纲：选择候选/填写自定义 → 点击 `生成大纲`（章数较多时会多轮调用模型，耗时更长但输出更稳；`?stream=1` 时可见分阶段进度）
- 逐章创作：点击 `续写下一章`（如需修改再对“最新章”做反馈重写）

如果你想用 Live Server 直接打开 `frontend/index.html`（仅开发调试更方便），注意 `index.html` 里只有在端口为 `5500` 时才会把 API 指向后端 `http://127.0.0.1:8000`，因此推荐用 `5500` 端口运行 Live Server。


## 🧭 下一步计划，可优化的地方

下面按“收益 vs 成本”列出可持续推进的方向（均基于当前代码结构与现有功能边界推导）。

1. 提升本地 RAG 质量与性能：当前使用的是本地 hash 嵌入函数（`rag/embedding.py`），可考虑增加更强的本地 embedding 实现或对检索结果做更精细的重排序；同时为 `write_chapter` 的检索结果加缓存（按 `project_id + current_chapter_index` + 参数组合），避免反复拼 prompt。
2. Markdown 渲染增强：前端 `renderMarkdown()` 目前属于轻量替换（标题/加粗/图片/段落），可补齐代码块、列表、链接、换行语义等，减少正文渲染差异。
3. 插图管线的工程化：`enable_chapter_illustrations` 目前是可选分支；可以进一步做图片缓存去重、失败兜底策略（例如联网失败时直接使用离线生成或返回占位）。
4. 更稳的并发与任务控制：当前按钮禁用依赖前端请求 pending；可在后端加上“同一 `project_id` 的写操作互斥锁/队列”，避免同时触发导致状态写回顺序问题。
5. 加强回归用例覆盖：现有 Playwright 已覆盖首页控件、按钮可见性与 stream NDJSON 消费等关键点；下一步可以补 `回滚 tail` 删除行为、`rewrite` 可选更新大纲路径、插图 enable 分支的 UI/后端接口联动回归（仍建议尽量避免依赖真实 LLM）。
6. 流式 NDJSON 背压：`?stream=1` 下 token 级进度极高频，后端 `ndjson_with_progress` 已用无界队列避免与慢客户端之间的死锁；若仍见“卡住”，可抓浏览器 Network 是否仍在收字节、后端日志是否停在某一 LLM 步骤。

