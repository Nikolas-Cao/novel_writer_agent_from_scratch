# 🚀 百万字分章小说 Agent（本地工作台）

这是一个本地“分章写小说”工作台：从创作意图生成剧情概要与结构化大纲；随后逐章续写、润色，并在最新章上支持反馈重写。写章会结合本地 RAG（检索前文摘要）与人物图谱上下文，来减少前后不一致。

详细实现与阶段验收见 `docs/项目实现学习指南.md`。

## 🎬 演示视频

基于本项目的实际操作录屏：[asserts/Novel_AI_Agent_Demo1.mp4](asserts/Novel_AI_Agent_Demo1.mp4)（克隆仓库后在本地播放器打开，或在 GitHub 上点击链接查看/下载原文件）。

## 🧩 项目简介（30 秒看懂）

- 从创作意图生成剧情概要与结构化大纲
- 逐章续写/润色；最新章支持反馈重写
- 结合本地 RAG 与人物图谱上下文保证连续性
- **同人知识库（可选）**：上传全局 `.txt` / `.md` 原著参考，项目勾选绑定；写纲/写章/润色时统一按激进路线仅注入分层摘要，**二创设定优先于原著**；大纲生成后绑定锁定


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
- `CHARACTER_GRAPH_RECENT_CHAPTERS`：写章时人物关系摘要的章节滑窗宽度（默认 5）
- `IMAGE_GEN_API_KEY` / `IMAGE_GEN_BASE_URL` / `IMAGE_GEN_MODEL` / `IMAGE_GEN_SIZE` / `IMAGE_GEN_TIMEOUT_S`：章节插图走 OpenAI Images（`IMAGE_GEN_API_KEY` 默认可回退 `OPENAI_API_KEY` / `PLANNER_API_KEY`）；生图失败则本张插图跳过
- `PLAN_OUTLINE_SINGLE_CALL_MAX`：超过该章节数则长篇大纲改为「极简骨架 + 滚动扩窗」策略（默认 16）
- `OUTLINE_SKELETON_BATCHES`：骨架分批生成的批次数（默认 5）
- `OUTLINE_SKELETON_RECENT_CONTEXT`：骨架分批时传给后续批次的最近章节摘要条数（默认 60）
- `OUTLINE_REPAIR_BACK_CHAPTERS`：扩窗时允许回修的前置章节数上限（默认 3；已写正文章节自动禁回修）
- `PLAN_OUTLINE_LARGE_BOOK_CHAPTERS`：达到该章节数后每章要点条数降为 2～3（默认 40）
- `OUTLINE_INITIAL_CHAPTERS`：历史兼容参数；当前 `/outline` 默认会先生成“全书骨架”（不再按该值截断章节数）
- `OUTLINE_WINDOW_SIZE`：续写过程中每次自动补齐的大纲窗口大小（默认 10）
- `OUTLINE_TRIGGER_MARGIN`：距离已生成大纲边界多少章时触发自动补窗（默认 0）
- `PROJECTS_ROOT` / `CHECKPOINT_DIR` / `VECTOR_STORE_DIR`：本地落盘根路径（默认分别为仓库下 `projects/`、`checkpoints/`、`vector_store/`）。**章节与人物图谱等在 `projects/{project_id}/`，Chroma 向量库在 `vector_store/{project_id}/`，API 状态 JSON 在 `checkpoints/api_state/{project_id}.json`**，与 `docs/项目实现学习指南.md` 一致。
- `DEBUG`：设为 `true/1/yes/on` 时，记录每次文本 LLM 调用到 `projects/{project_id}/llm_invoke_results/`。文件名为 `{utc_timestamp}_{status}_{purpose}_{short_id}.json`（`status=success/error`，`purpose` 为细粒度业务目的）。流式调用会在结束后聚合完整输出再写入单个 JSON。
- **全局知识库摄取与检索**：`KB_CHUNK_TARGET_CHARS` / `KB_CHUNK_OVERLAP_CHARS` / `KB_INGEST_BATCH_CHUNKS` / `KB_READ_BLOCK_BYTES` / `KB_MAX_CHUNKS_PER_DOCUMENT`；摘要资产 map-reduce：`KB_ASSET_LEAF_BATCH_CHARS` / `KB_ASSET_MAX_LEAF_WINDOWS`；检索：`KB_RETRIEVE_CHROMA_K` / `KB_RETRIEVE_FTS_K` / `KB_RETRIEVE_FINAL_K`；低置信度补查：`KB_TOOL_LOOP_MAX_CALLS`（详见 `config.py`）。

</details>

### 2. 启动后端（FastAPI）

安装 Python 依赖：

```bash
pip install -r requirements.txt
```

（上传知识库文档使用 `multipart/form-data`，依赖已包含 `python-multipart`。）

运行服务：

```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```

启动后打开：

- `http://127.0.0.1:8000/` 或 `http://127.0.0.1:8000/app`（后端挂载同源静态前端）
- 章节插图文件通过 **`/project-data/`** 静态访问（对应 `PROJECTS_ROOT` 下相对路径）

你不需要单独“启动/构建前端”。只要后端已运行，就可以直接在浏览器中使用 UI：

- **（可选）同人知识库**：左侧「同人知识库」创建知识集、上传 `.txt`/`.md`；在**尚未生成大纲**的项目上勾选「绑定当前项目」（或在点击「生成概要」创建新项目前勾好，会随 `POST /projects` 一并提交）；生成大纲后绑定不可改
- 新建项目：输入创作意图 → 点击 `生成概要`
- 项目管理：在「项目列表」中对任一项目右键，可执行 `重命名`（设置项目昵称，列表优先显示昵称）和 `删除项目`（硬删除项目全部私有资源）
- 选概要并生成大纲：选择候选/填写自定义 → 点击 `生成大纲`（默认先按 5 批循环生成全书骨架：每章 `title + description`，并做索引覆盖校验与失败批占位兜底；随后先扩写首个 `OUTLINE_WINDOW_SIZE` 窗口；长耗时接口可带查询参数 **`stream=1` 或 `stream=true`**，返回 NDJSON 进度流）
- 逐章创作：点击 `续写下一章`（可选勾选 **「开启图片生成」**；当写到大纲边界会自动补齐后续 `OUTLINE_WINDOW_SIZE` 章，并用最近摘要/大纲要点/人物关系保证连贯）
- 事件日志：在项目详情点击 `查看事件日志`，弹窗查看该项目的重要事件（如生成概要/大纲、续写、反馈重写）

如果你想用 Live Server 直接打开 `frontend/index.html`（仅开发调试更方便），注意 `index.html` 里只有在端口为 `5500` 时才会把 API 指向后端 `http://127.0.0.1:8000`，因此推荐用 `5500` 端口运行 Live Server。

<details><summary>行为说明（与实现对齐）</summary>

- **`GET /projects` 前的空项目清理**：创建超过约 **10 分钟**，且 state 中 **无大纲、无章节**（含磁盘上无章节 `.md`），且 **instruction / plot_ideas / selected_plot_summary 均为空** 的项目会被自动删除，并移除对应 `vector_store/{project_id}` 目录。详见学习指南「后端 API」一节。
- **项目昵称与显示名**：项目保留内部 ID（`p-uuid`）作为稳定标识，同时支持可选 `nickname`；前端显示规则为 `nickname || project_id`，清空昵称后回退显示 `project_id`。
- **项目硬删除**：`DELETE /projects/{project_id}` 会删除 `checkpoints/api_state/{project_id}.json`、`projects/{project_id}`（章节/图谱/插图）和 `vector_store/{project_id}`。
- **项目事件日志**：关键写作流程会写入 `projects/{project_id}/event_logs.ndjson`（默认记录 `event_name` + 简短 `event_content`，并附带时间/状态/章节号）；可通过 `GET /projects/{project_id}/events` 查询。
- **流式响应**：部分接口支持 `stream` 查询参数；NDJSON 进度通道在服务端使用**无界** `asyncio.Queue`，避免 token 高频进度与慢客户端之间的死锁。**反馈重写**在 `stream=1` 时仅对润色阶段推送正文 token 流（`refine_chapter_stream`），按反馈重写阶段不推送 token 流。

</details>

<details><summary>前端 E2E（Playwright，可选）</summary>

在仓库根目录安装 Node 依赖后执行：

```bash
npm install
npm run test:ui
```

默认需已手动启动后端（如 `http://127.0.0.1:8000`）。用例位于 `e2e/`，包含 `smoke.spec.ts`、`regression.spec.ts`、`stream-progress.spec.ts` 等。

</details>


## 🧭 下一步计划，可优化的地方

下面按“收益 vs 成本”列出可持续推进的方向（均基于当前代码结构与现有功能边界推导）。

1. 提升本地 RAG 质量与性能：当前使用的是本地 hash 嵌入函数（`rag/embedding.py`），可考虑增加更强的本地 embedding 实现或对检索结果做更精细的重排序；同时为 `write_chapter` 的检索结果加缓存（按 `project_id + current_chapter_index` + 参数组合），避免反复拼 prompt。
2. Markdown 渲染增强：前端 `renderMarkdown()` 目前属于轻量替换（标题/加粗/图片/段落），可补齐代码块、列表、链接、换行语义等，减少正文渲染差异。
3. 插图管线的工程化：当前为「单张插图 + OpenAI 生图」，失败即跳过；策划侧强调情节相关锚点与大纲上下文，插入侧对锚点做多策略匹配，减少图总落在文末；可进一步做缓存去重、多图、或非 OpenAI 后端适配。
4. 更稳的并发与任务控制：当前按钮禁用依赖前端请求 pending；可在后端加上“同一 `project_id` 的写操作互斥锁/队列”，避免同时触发导致状态写回顺序问题。
5. 加强回归用例覆盖：现有 Playwright（`e2e/smoke.spec.ts`、`regression.spec.ts`、`stream-progress.spec.ts` 等）已覆盖首页控件、按钮可见性与 NDJSON 流式消费等关键点；下一步可以补 `回滚 tail` 删除行为、`rewrite` 可选更新大纲路径、插图 enable 分支的 UI/后端接口联动回归（仍建议尽量避免依赖真实 LLM）。
6. 流式 NDJSON 背压：`stream=1` / `stream=true` 下 token 级进度极高频，后端 `ndjson_with_progress` 已用无界队列避免与慢客户端之间的死锁；若仍见“卡住”，可抓浏览器 Network 是否仍在收字节、后端日志是否停在某一 LLM 步骤。

