const API_BASE = typeof window !== "undefined" && window.API_BASE ? window.API_BASE : "";

function installLiveServerReloadGuard() {
  if (typeof window === "undefined") return;
  const isLocalDevPort = window.location && window.location.port === "5500";
  if (!isLocalDevPort || typeof window.WebSocket !== "function") return;

  const NativeWebSocket = window.WebSocket;
  const shouldBlockLiveReloadSocket = (url) => {
    try {
      const parsed = new URL(String(url), window.location.href);
      const isWs = parsed.protocol === "ws:" || parsed.protocol === "wss:";
      const sameHost = parsed.hostname === window.location.hostname;
      const isLiveReloadEndpoint = /\/ws$/i.test(parsed.pathname || "");
      return isWs && sameHost && isLiveReloadEndpoint;
    } catch (_) {
      return false;
    }
  };

  function WrappedWebSocket(url, protocols) {
    if (shouldBlockLiveReloadSocket(url)) {
      // 阻止 Live Server 自动热重载，避免后端写文件时前端被整页刷新。
      return {
        readyState: NativeWebSocket.CLOSED,
        close: () => {},
        send: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
        onopen: null,
        onmessage: null,
        onerror: null,
        onclose: null,
      };
    }
    if (typeof protocols === "undefined") {
      return new NativeWebSocket(url);
    }
    return new NativeWebSocket(url, protocols);
  }

  WrappedWebSocket.prototype = NativeWebSocket.prototype;
  WrappedWebSocket.CONNECTING = NativeWebSocket.CONNECTING;
  WrappedWebSocket.OPEN = NativeWebSocket.OPEN;
  WrappedWebSocket.CLOSING = NativeWebSocket.CLOSING;
  WrappedWebSocket.CLOSED = NativeWebSocket.CLOSED;
  window.WebSocket = WrappedWebSocket;
}

installLiveServerReloadGuard();
function apiUrl(url) {
  return API_BASE + url;
}
const api = {
  get: (url) => {
    const full = apiUrl(url);
    console.log("[API] GET", full);
    return fetch(full)
      .catch((err) => {
        throw normalizeNetworkError(err, full);
      })
      .then(ensureOk)
      .then((r) => r.json());
  },
  post: (url, body) => {
    const full = apiUrl(url);
    console.log("[API] POST", full, body);
    return fetch(full, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    })
      .catch((err) => {
        throw normalizeNetworkError(err, full);
      })
      .then(ensureOk)
      .then((r) => r.json());
  },
  delete: (url) => {
    const full = apiUrl(url);
    console.log("[API] DELETE", full);
    return fetch(full, { method: "DELETE" })
      .catch((err) => {
        throw normalizeNetworkError(err, full);
      })
      .then(ensureOk)
      .then((r) => r.json());
  },
};

function normalizeNetworkError(err, fullUrl) {
  const msg = (err && err.message) || "";
  if (msg === "Failed to fetch" || err instanceof TypeError) {
    return new Error(
      `无法连接后端（${fullUrl}）。请确认后端已启动：py -m uvicorn server:app --host 127.0.0.1 --port 8000`
    );
  }
  return err instanceof Error ? err : new Error(String(err));
}

function ensureOk(resp) {
  if (!resp.ok) {
    return resp.text().then((t) => {
      throw new Error(extractBackendErrorMessage(resp, t));
    });
  }
  return resp;
}

/** 追加 stream=1，消费 NDJSON 进度行；最后一行为 {"type":"result","body":...} */
async function postNdjsonStream(urlPath, body, onProgress) {
  const sep = urlPath.includes("?") ? "&" : "?";
  const full = apiUrl(`${urlPath}${sep}stream=1`);
  console.log("[API] POST stream", full, body);
  const resp = await fetch(full, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  }).catch((err) => {
    throw normalizeNetworkError(err, full);
  });
  if (!resp.ok) {
    const t = await resp.text();
    throw new Error(extractBackendErrorMessage(resp, t));
  }
  const ct = ((resp.headers && resp.headers.get("content-type")) || "").toLowerCase();
  if (!ct.includes("ndjson")) {
    return resp.json();
  }
  const reader = resp.body && resp.body.getReader ? resp.body.getReader() : null;
  if (!reader) {
    throw new Error("浏览器不支持流式读取响应");
  }
  const dec = new TextDecoder();
  let buf = "";
  let finalBody = null;
  while (true) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buf += dec.decode(chunk.value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop() || "";
    for (const line of lines) {
      const s = line.trim();
      if (!s) continue;
      let o;
      try {
        o = JSON.parse(s);
      } catch (_) {
        continue;
      }
      if (o.type === "progress" && typeof onProgress === "function") {
        onProgress(o);
      } else if (o.type === "result") {
        finalBody = o.body;
      } else if (o.type === "error") {
        throw new Error((o.detail && String(o.detail)) || "请求失败");
      }
    }
  }
  if (finalBody === null) {
    throw new Error("流式响应未返回结果");
  }
  return finalBody;
}

function applyProgressToStatus(evt) {
  const msg = (evt && evt.message) || "";
  if (msg) {
    setStatus(msg);
    return;
  }
  const stage = (evt && evt.stage) || "";
  if (stage) {
    setStatus(`处理中：${stage}`);
  }
}

/**
 * 章节流式正文（refine / rewrite）进度回调。
 * 说明：NDJSON 可能在单次 read 内被同步解析多行，若用时间节流会漏掉尾部；后续 pipeline 阶段不再推送流式 delta，
 * 必须在收到非流式 progress 时 flush，且在 postNdjsonStream 返回后 flushNow，避免 rAF 晚于 openChapter。
 */
function createChapterStreamProgressHandler(streamStage) {
  let streamBuffer = "";
  let rafId = null;

  function paint() {
    if (el.chapterView) {
      el.chapterView.innerHTML = `<pre class="streaming-pre">${escapeHtml(streamBuffer)}</pre>`;
    }
  }

  function flushNow() {
    if (rafId != null && typeof cancelAnimationFrame === "function") {
      cancelAnimationFrame(rafId);
      rafId = null;
    }
    paint();
  }

  function schedulePaint() {
    if (rafId != null) return;
    if (typeof requestAnimationFrame !== "function") {
      paint();
      return;
    }
    rafId = requestAnimationFrame(() => {
      rafId = null;
      paint();
    });
  }

  function onProgress(evt) {
    const stage = evt && evt.stage ? String(evt.stage) : "";
    const delta = evt && evt.message ? String(evt.message) : "";
    if (stage === streamStage && delta) {
      streamBuffer += delta;
      schedulePaint();
      return;
    }
    flushNow();
    applyProgressToStatus(evt);
  }

  return { onProgress, flushNow };
}

function extractBackendErrorMessage(resp, rawText) {
  const text = String(rawText || "").trim();
  if (!text) return `HTTP ${resp.status}`;
  const contentType = (resp.headers && resp.headers.get("content-type")) || "";
  if (contentType.includes("application/json")) {
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed.detail === "string" && parsed.detail.trim()) {
        return parsed.detail.trim();
      }
      if (parsed && typeof parsed.message === "string" && parsed.message.trim()) {
        return parsed.message.trim();
      }
    } catch (_) {
      // JSON 解析失败时继续走文本兜底。
    }
  }
  // 兜底：移除 HTML 标签，避免把整段错误页面灌进状态栏。
  return text.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
}

const state = {
  currentProjectId: null,
  projectIds: [],
  /** @type {Record<string, number>} */
  projectCreatedAt: {},
  projectPreviews: {},
  plotIdeas: [],
  selectedIdea: "",
  expandedIdeaIndex: null,
  currentChapterIndex: null,
  selectedChapterIndex: null,
  chapterMetas: [],
  currentChapterCharacterGraph: null,
  isGeneratingOutline: false,
  isChapterWriteInProgress: false,
  /** 用于防止 openChapter 的请求乱序覆盖 UI */
  openChapterRequestSeq: 0,
};

const el = {
  layout: document.querySelector(".layout"),
  status: document.getElementById("global-status"),
  panelCreate: document.querySelector(".panel-create"),
  panelDetail: document.querySelector(".panel-detail"),
  plotIdeasSection: document.getElementById("plot-ideas-section"),
  projectList: document.getElementById("project-list"),
  instruction: document.getElementById("instruction-input"),
  plotIdeas: document.getElementById("plot-ideas"),
  customSummary: document.getElementById("custom-summary-input"),
  totalChapters: document.getElementById("total-chapters-input"),
  selectedIdeaView: document.getElementById("selected-idea-view"),
  projectMeta: document.getElementById("project-meta"),
  tokenUsage: document.getElementById("token-usage"),
  outlineView: document.getElementById("outline-view"),
  chapterList: document.getElementById("chapter-list"),
  chapterView: document.getElementById("chapter-view"),
  chapterModal: document.getElementById("chapter-modal"),
  chapterModalContent: document.getElementById("chapter-modal-content"),
  characterGraphModal: document.getElementById("character-graph-modal"),
  characterGraphModalContent: document.getElementById("character-graph-modal-content"),
  btnViewChapterModal: document.getElementById("btn-view-chapter-modal"),
  btnViewCharacterGraphModal: document.getElementById("btn-view-character-graph-modal"),
  btnCloseChapterModal: document.getElementById("btn-close-chapter-modal"),
  btnCloseCharacterGraphModal: document.getElementById("btn-close-character-graph-modal"),
  feedbackRewriteSection: document.getElementById("feedback-rewrite-section"),
  feedback: document.getElementById("feedback-input"),
  updateOutline: document.getElementById("update-outline-checkbox"),
  btnRefreshProjects: document.getElementById("btn-refresh-projects"),
  btnNewProject: document.getElementById("btn-new-project"),
  btnGenerateIdeas: document.getElementById("btn-generate-ideas"),
  btnGenerateOutline: document.getElementById("btn-generate-outline"),
  btnNextChapter: document.getElementById("btn-next-chapter"),
  btnRollbackTail: document.getElementById("btn-rollback-tail"),
  btnRegenerateChapter: document.getElementById("btn-regenerate-chapter"),
  btnRewrite: document.getElementById("btn-rewrite"),
};

function buildChapterCharacterGraphHtml(data, chapterIndex) {
  const graph = (data && data.character_graph) || { nodes: [], edges: [] };
  const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
  const edges = Array.isArray(graph.edges) ? graph.edges : [];

  if (chapterIndex === null || chapterIndex === undefined) {
    return "<p>请先在章节列表中打开一个章节</p>";
  }
  if (!nodes.length && !edges.length) {
    return "<p>当前章节暂无人物关系数据</p>";
  }

  const idToName = {};
  nodes.forEach((n) => {
    if (n && n.id !== undefined && n.id !== null) {
      idToName[String(n.id)] = String(n.name || n.id);
    }
  });

  const nodeItems = nodes
    .map((n) => {
      const name = escapeHtml(String((n && (n.name || n.id)) || "未命名人物"));
      const desc = escapeHtml(String((n && n.description) || ""));
      return `<li><strong>${name}</strong>${desc ? `：${desc}` : ""}</li>`;
    })
    .join("");

  const edgeItems = edges
    .map((e) => {
      const fromName = escapeHtml(String(idToName[String((e && e.from_id) || "")] || (e && e.from_id) || "未知"));
      const toName = escapeHtml(String(idToName[String((e && e.to_id) || "")] || (e && e.to_id) || "未知"));
      const relation = escapeHtml(String((e && e.relation) || "相关"));
      const note = escapeHtml(String((e && e.note) || ""));
      return `<li>${fromName} -> ${toName}（${relation}）${note ? ` [${note}]` : ""}</li>`;
    })
    .join("");

  return `
    <div class="graph-meta">第 ${Number(chapterIndex) + 1} 章 · 人物 ${nodes.length} · 关系 ${edges.length}</div>
    <div class="graph-columns">
      <div class="graph-col">
        <h4>人物</h4>
        <ul>${nodeItems || "<li>暂无人物</li>"}</ul>
      </div>
      <div class="graph-col">
        <h4>关系</h4>
        <ul>${edgeItems || "<li>暂无关系</li>"}</ul>
      </div>
    </div>
  `;
}

async function loadChapterCharacterGraph(index) {
  if (!state.currentProjectId) return;
  if (index === null || index === undefined) {
    state.currentChapterCharacterGraph = null;
    return;
  }
  try {
    const data = await api.get(
      `/projects/${state.currentProjectId}/character-graph?chapter_index=${Number(index)}`
    );
    state.currentChapterCharacterGraph = data;
    return data;
  } catch (_) {
    // 图谱加载失败不阻断正文阅读，面板展示兜底文案即可。
    state.currentChapterCharacterGraph = null;
    return null;
  }
}

function isLatestChapterSelected() {
  if (state.selectedChapterIndex === null) return false;
  if (!Array.isArray(state.chapterMetas) || state.chapterMetas.length === 0) return false;
  const latest = state.chapterMetas.reduce((max, item) => Math.max(max, Number(item.index)), -1);
  return Number(state.selectedChapterIndex) === Number(latest);
}

function setChapterWriteButtonsDisabled(disabled) {
  if (el.btnNextChapter) el.btnNextChapter.disabled = disabled;
  if (el.btnRegenerateChapter) el.btnRegenerateChapter.disabled = disabled;
  if (el.btnRewrite) el.btnRewrite.disabled = disabled;
}

function updateChapterActionButtons() {
  const hasSelection = state.selectedChapterIndex !== null;
  const latestSelected = isLatestChapterSelected();
  const canModify = hasSelection && latestSelected;
  // 仅在选中「最新章节」时显示：续写下一章、重新生成本章、提交反馈重写
  const showWriteActions = !hasSelection || latestSelected;
  const writeDisabled = state.isChapterWriteInProgress;

  if (el.btnNextChapter) {
    el.btnNextChapter.hidden = !showWriteActions;
    el.btnNextChapter.disabled = writeDisabled;
  }
  if (el.btnRegenerateChapter) {
    el.btnRegenerateChapter.hidden = !canModify;
    el.btnRegenerateChapter.disabled = writeDisabled || !canModify;
    el.btnRegenerateChapter.title = canModify ? "" : "仅支持对最新章节操作，避免后续章节逻辑断裂";
  }
  if (el.btnRewrite) {
    el.btnRewrite.hidden = !canModify;
    el.btnRewrite.disabled = writeDisabled || !canModify;
    el.btnRewrite.title = canModify ? "" : "仅支持对最新章节操作，避免后续章节逻辑断裂";
  }
  if (el.btnRollbackTail) {
    // 仅在选中非最新章节时显示（最新章节后无内容可回滚）
    el.btnRollbackTail.hidden = !hasSelection || latestSelected;
    el.btnRollbackTail.disabled = !hasSelection;
    el.btnRollbackTail.title = hasSelection ? "" : "请先在章节列表中打开一个章节";
  }
  if (el.feedbackRewriteSection) {
    el.feedbackRewriteSection.hidden = !canModify;
  }
}

function setPlotIdeasSectionVisibility(visible) {
  if (!el.plotIdeasSection) return;
  el.plotIdeasSection.hidden = !visible;
}

function hasOutlineData(projectData) {
  const volumes =
    (projectData &&
      projectData.outline_structure &&
      Array.isArray(projectData.outline_structure.volumes) &&
      projectData.outline_structure.volumes) ||
    [];
  const hasOutlineVolumes = volumes.length > 0;
  const hasOutlineText =
    typeof (projectData && projectData.outline) === "string" &&
    projectData.outline.trim().length > 0;
  const hasChapters = Array.isArray(projectData && projectData.chapters) && projectData.chapters.length > 0;
  return hasOutlineVolumes || hasOutlineText || hasChapters;
}

function updateCreatePanelVisibility(projectData) {
  if (!el.panelCreate) return;
  const shouldHide = hasOutlineData(projectData);
  el.panelCreate.hidden = shouldHide;
  if (el.layout) {
    el.layout.classList.toggle("is-create-hidden", shouldHide);
  }
}

function updateDetailLayout(projectData) {
  if (!el.panelDetail) return;
  el.panelDetail.classList.toggle("has-outline", hasOutlineData(projectData));
}

function setDetailPanelVisibility(visible) {
  if (!el.panelDetail) return;
  el.panelDetail.hidden = !visible;
  if (el.layout) {
    el.layout.classList.toggle("is-detail-hidden", !visible);
  }
  if (!visible && el.chapterView) {
    el.chapterView.style.height = "";
  }
}

function adjustChapterViewHeight() {
  if (!el.chapterView) return;
  // 有大纲时由 CSS flex 控制 #chapter-view 高度，填满右侧剩余空间，不再设置固定高度
  if (el.panelDetail && el.panelDetail.classList.contains("has-outline")) {
    el.chapterView.style.height = "";
    return;
  }
  el.chapterView.style.height = "";
}

function setChapterNavigationLocked(locked) {
  // 只锁“章节切换入口”，保留脚本内部 openChapter 的能力（脚本会在锁定期间主动调用 openChapter）
  if (el.chapterList) {
    el.chapterList.style.pointerEvents = locked ? "none" : "";
    el.chapterList.style.opacity = locked ? "0.6" : "";
  }
  if (el.outlineView) {
    el.outlineView.style.pointerEvents = locked ? "none" : "";
    el.outlineView.style.opacity = locked ? "0.75" : "";
  }
}

function setGlobalInteractionLocked(locked) {
  // 锁定项目级入口，避免在章节生成链路中切换项目导致状态错配。
  if (el.projectList) {
    el.projectList.style.pointerEvents = locked ? "none" : "";
    el.projectList.style.opacity = locked ? "0.6" : "";
  }
  const targets = [
    el.btnRefreshProjects,
    el.btnNewProject,
    el.btnGenerateIdeas,
    el.btnGenerateOutline,
  ];
  targets.forEach((node) => {
    if (node) node.disabled = Boolean(locked);
  });
}

function setStatus(msg) {
  if (!el.status) return;
  el.status.textContent = msg || "";
  el.status.classList.remove("is-error", "is-working", "is-ok");
  if (!msg) return;
  if (msg.includes("失败") || msg.includes("错误")) {
    el.status.classList.add("is-error");
  } else if (
    msg.includes("中") ||
    msg.includes("加载") ||
    msg.includes("生成") ||
    msg.includes("重写")
  ) {
    el.status.classList.add("is-working");
  } else {
    el.status.classList.add("is-ok");
  }
}

function escapeHtml(text) {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function formatCreatedAt(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "";
  const d = new Date(n * 1000);
  const pad = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function formatTokenCount(n) {
  const x = Number(n);
  if (!Number.isFinite(x)) return "0";
  return x.toLocaleString("zh-CN");
}

function renderTokenUsage(project) {
  if (!el.tokenUsage) return;
  const raw = project && project.token_usage;
  const usage = raw && typeof raw === "object" ? raw : {};
  const keys = Object.keys(usage);
  if (!keys.length) {
    el.tokenUsage.innerHTML = "<strong>Token 用量</strong>：暂无累计（完成一次需 LLM 的操作后显示）";
    return;
  }
  const rows = keys
    .map((model) => {
      const u = usage[model] || {};
      const inp = formatTokenCount(u.input_tokens);
      const out = formatTokenCount(u.output_tokens);
      return `<li><code>${escapeHtml(String(model))}</code>：输入 ${inp} / 输出 ${out}</li>`;
    })
    .join("");
  el.tokenUsage.innerHTML = `<strong>Token 用量</strong><ul class="token-usage-list">${rows}</ul>`;
}

function renderMarkdown(md) {
  // 先处理转义，再回填 markdown 语法
  let html = escapeHtml(md || "");
  html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>");
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img alt="$1" src="$2" />');
  html = html.replace(/\n\n+/g, "</p><p>");
  return `<p>${html}</p>`;
}

function setOutlineGeneratingUI(isGenerating) {
  state.isGeneratingOutline = Boolean(isGenerating);
  const lockTargets = [
    el.instruction,
    el.btnGenerateIdeas,
    el.customSummary,
    el.btnGenerateOutline,
  ];
  lockTargets.forEach((node) => {
    if (node) node.disabled = state.isGeneratingOutline;
  });
  if (el.plotIdeas) {
    el.plotIdeas.classList.toggle("is-disabled", state.isGeneratingOutline);
  }
}

function renderProjectList(projectIds) {
  state.projectIds = projectIds || [];
  el.projectList.innerHTML = "";

  if (!state.projectIds.length) {
    const li = document.createElement("li");
    li.className = "project-item-empty";
    li.textContent = "暂无项目";
    el.projectList.appendChild(li);
    return;
  }

  for (const projectId of state.projectIds) {
    const li = document.createElement("li");
    li.className = "project-item";
    const isSelected = state.currentProjectId === projectId;
    if (isSelected) {
      li.classList.add("expanded");
    }
    if (isSelected) {
      li.classList.add("selected");
      li.setAttribute("aria-current", "true");
    }

    const head = document.createElement("div");
    head.className = "project-item-head";

    const btn = document.createElement("button");
    btn.className = "project-open-btn";
    const ts = state.projectCreatedAt[projectId];
    const timeLabel = formatCreatedAt(ts);
    btn.textContent = timeLabel ? `打开 ${projectId}（${timeLabel}）` : `打开 ${projectId}`;
    btn.onclick = () => openProject(projectId);
    head.appendChild(btn);

    if (isSelected) {
      const tag = document.createElement("span");
      tag.className = "project-selected-tag";
      tag.textContent = "已选中";
      head.appendChild(tag);
    }
    li.appendChild(head);

    if (isSelected) {
      const preview = document.createElement("div");
      preview.className = "project-preview";
      const pv = state.projectPreviews[projectId];
      let displayText = "正在加载概要...";
      let fullText = "";
      if (pv != null && pv !== "") {
        if (typeof pv === "string") {
          displayText = pv;
          fullText = pv;
        } else {
          displayText = pv.display || "正在加载概要...";
          fullText = pv.full != null && pv.full !== "" ? pv.full : displayText;
        }
      }
      preview.textContent = displayText;
      if (fullText && fullText !== displayText) {
        preview.setAttribute("title", fullText);
        preview.classList.add("project-preview--truncated");
      } else {
        preview.removeAttribute("title");
        preview.classList.remove("project-preview--truncated");
      }
      li.appendChild(preview);
    }

    el.projectList.appendChild(li);
  }
}

function buildProjectPreview(project) {
  const summary = String((project && project.selected_plot_summary) || "").trim();
  if (summary) {
    const full = `概要：${summary}`;
    const display =
      summary.length > 100 ? `概要：${summary.slice(0, 100)}...` : full;
    return { display, full };
  }
  const outline = (project && project.outline_structure && project.outline_structure.volumes) || [];
  if (outline.length > 0) {
    const chapterTitles = [];
    for (const volume of outline) {
      for (const chapter of volume.chapters || []) {
        if (chapter && chapter.title) chapterTitles.push(chapter.title);
        if (chapterTitles.length >= 2) break;
      }
      if (chapterTitles.length >= 2) break;
    }
    let text;
    if (chapterTitles.length > 0) {
      text = `概要：${chapterTitles.join("、")}`;
    } else {
      text = `概要：${outline[0].volume_title || "已生成大纲"}`;
    }
    return { display: text, full: text };
  }
  const instruction = String((project && project.instruction) || "").trim();
  if (instruction) {
    const text = `创作意图：${instruction}`;
    return { display: text, full: text };
  }
  const fallback = "暂无概要，点击后可继续生成。";
  return { display: fallback, full: fallback };
}

function parseTotalChaptersInput() {
  if (!el.totalChapters) return null;
  const raw = String(el.totalChapters.value || "").trim();
  if (!raw) return null;
  const n = Number(raw);
  if (!Number.isInteger(n) || n <= 0) {
    throw new Error("目标章节数量必须是大于 0 的整数");
  }
  return n;
}

async function loadProjects() {
  setStatus("加载项目列表...");
  try {
    const data = await api.get("/projects");
    const projects = data.projects || [];
    const nextCreated = { ...state.projectCreatedAt };
    for (const item of projects) {
      const pid = item && item.project_id;
      if (!pid) continue;
      if (item.created_at != null) nextCreated[pid] = Number(item.created_at);
    }
    state.projectCreatedAt = nextCreated;
    const projectIds = projects.map((item) => item.project_id).filter(Boolean);
    if (state.currentProjectId && !projectIds.includes(state.currentProjectId)) {
      state.currentProjectId = null;
    }
    if (!state.currentProjectId) {
      updateCreatePanelVisibility(null);
      updateDetailLayout(null);
      setDetailPanelVisibility(false);
    }
    renderProjectList(projectIds);
    setStatus("项目列表已更新");
  } catch (e) {
    setStatus(`加载失败：${e.message}`);
  }
}

async function openProject(projectId) {
  state.currentProjectId = projectId;
  // 切换项目/重新打开项目时，避免遗留上一项目的反馈输入与勾选状态。
  if (el.feedback) el.feedback.value = "";
  if (el.updateOutline) el.updateOutline.checked = false;
  renderProjectList(state.projectIds);
  setStatus(`打开项目 ${projectId} ...`);
  try {
    const p = await api.get(`/projects/${projectId}`);
    const showDetail = hasOutlineData(p);
    const chapters = p.chapters || [];
    state.chapterMetas = chapters;
    const generatedCount = chapters.length;
    const rawCurrentIndex = Number.isFinite(p.current_chapter_index) ? Number(p.current_chapter_index) : null;
    const resolvedCurrentIndex =
      generatedCount > 0
        ? rawCurrentIndex !== null && rawCurrentIndex >= 0 && rawCurrentIndex < generatedCount
          ? rawCurrentIndex
          : generatedCount - 1
        : null;
    const currentIndexOneBased = generatedCount > 0 && resolvedCurrentIndex !== null ? resolvedCurrentIndex + 1 : 0;
    updateCreatePanelVisibility(p);
    updateDetailLayout(p);
    setDetailPanelVisibility(showDetail);
    state.currentChapterIndex = resolvedCurrentIndex;
    state.selectedChapterIndex = null;
    updateChapterActionButtons();
    if (el.totalChapters && Number.isFinite(p.total_chapters)) {
      el.totalChapters.value = String(p.total_chapters);
    }
    state.projectPreviews[projectId] = buildProjectPreview(p);
    if (p.created_at != null) state.projectCreatedAt[projectId] = Number(p.created_at);
    renderProjectList(state.projectIds);
    el.projectMeta.textContent = `项目：${projectId} | 目标章节：${p.total_chapters || "-"} | 已生成：${generatedCount}章 | 当前章节：${currentIndexOneBased}`;
    renderTokenUsage(p);
    renderOutline(p.outline_structure);
    renderChapterList(chapters);
    adjustChapterViewHeight();
    if (generatedCount > 0 && resolvedCurrentIndex !== null) {
      await openChapter(resolvedCurrentIndex);
      setStatus(`已打开 ${projectId}，已定位到第 ${resolvedCurrentIndex + 1} 章`);
    } else if (el.chapterView) {
      el.chapterView.innerHTML = "<p>暂无章节，请先点击“续写下一章”</p>";
      adjustChapterViewHeight();
      setStatus(`已打开 ${projectId}，当前暂无章节`);
    } else {
      setStatus(`已打开 ${projectId}`);
    }
  } catch (e) {
    state.currentChapterIndex = null;
    if (el.tokenUsage) el.tokenUsage.innerHTML = "";
    updateCreatePanelVisibility(null);
    updateDetailLayout(null);
    setDetailPanelVisibility(false);
    setStatus(`打开失败：${e.message}`);
  }
}

async function refreshProjectsAndHideDetail() {
  state.currentProjectId = null;
  state.currentChapterIndex = null;
  state.selectedChapterIndex = null;
  state.chapterMetas = [];
  renderProjectList(state.projectIds);
  updateCreatePanelVisibility(null);
  updateDetailLayout(null);
  setDetailPanelVisibility(false);
  await loadProjects();
}

function startNewProject() {
  state.currentProjectId = null;
  state.currentChapterIndex = null;
  state.selectedChapterIndex = null;
  state.chapterMetas = [];
  state.plotIdeas = [];
  state.selectedIdea = "";
  state.expandedIdeaIndex = null;
  renderProjectList(state.projectIds);
  updateCreatePanelVisibility(null);
  updateDetailLayout(null);
  setDetailPanelVisibility(false);
  setPlotIdeasSectionVisibility(false);
  setStatus("请输入创作意图后，点击「生成概要」（或先「新建项目」再生成）");
  if (el.instruction) el.instruction.focus();
}

function renderPlotIdeas(ideas) {
  state.plotIdeas = ideas || [];
  setPlotIdeasSectionVisibility(state.plotIdeas.length > 0);
  if (
    state.expandedIdeaIndex !== null &&
    (state.expandedIdeaIndex < 0 || state.expandedIdeaIndex >= state.plotIdeas.length)
  ) {
    state.expandedIdeaIndex = null;
  }
  // 默认选中第一条，确保有明确可选结果
  if (!state.selectedIdea && state.plotIdeas.length > 0) {
    state.selectedIdea = state.plotIdeas[0];
  }

  el.plotIdeas.innerHTML = "";
  (state.plotIdeas || []).forEach((idea, idx) => {
    const card = document.createElement("div");
    card.className = "card";
    if (state.isGeneratingOutline) card.classList.add("is-disabled");
    if (state.selectedIdea === idea) card.classList.add("selected");
    if (state.expandedIdeaIndex === idx) card.classList.add("expanded");
    card.innerHTML = `
      <div class="card-head">
        <span class="card-radio"></span>
        <strong>候选 ${idx + 1}</strong>
      </div>
      <div class="card-body">${escapeHtml(idea)}</div>
    `;
    card.onclick = () => {
      if (state.isGeneratingOutline) return;
      state.expandedIdeaIndex = state.expandedIdeaIndex === idx ? null : idx;
      state.selectedIdea = idea;
      renderPlotIdeas(state.plotIdeas);
    };
    el.plotIdeas.appendChild(card);
  });

  if (!state.plotIdeas.length) {
    el.plotIdeas.innerHTML = "";
    el.selectedIdeaView.textContent = "当前未选择剧情概要";
    return;
  }

  if (state.selectedIdea) {
    el.selectedIdeaView.textContent = `当前已选：${state.selectedIdea.slice(0, 80)}${state.selectedIdea.length > 80 ? "..." : ""}`;
  } else {
    el.selectedIdeaView.textContent = "当前未选择剧情概要";
  }
}

function renderOutline(outlineStructure) {
  const volumes = (outlineStructure && outlineStructure.volumes) || [];
  if (!volumes.length) {
    el.outlineView.innerHTML = "<p>暂无大纲</p>";
    return;
  }
  const chunks = [];
  const activeChapterIndex =
    state.selectedChapterIndex !== null && Number.isFinite(Number(state.selectedChapterIndex))
      ? Number(state.selectedChapterIndex)
      : state.currentChapterIndex !== null && Number.isFinite(Number(state.currentChapterIndex))
        ? Number(state.currentChapterIndex)
        : null;
  let chapterSeq = 0;
  volumes.forEach((v, vi) => {
    const chaptersInVolume = (v.chapters || []).length;
    const startChapterIdx = chapterSeq;
    const endChapterIdx = chapterSeq + chaptersInVolume - 1;
    const isActiveVolume =
      activeChapterIndex !== null &&
      chaptersInVolume > 0 &&
      activeChapterIndex >= startChapterIdx &&
      activeChapterIndex <= endChapterIdx;
    const shouldOpen = isActiveVolume;
    chunks.push(`<details${shouldOpen ? " open" : ""}><summary>${escapeHtml(v.volume_title || `卷${vi + 1}`)}</summary>`);
    (v.chapters || []).forEach((c, ci) => {
      const isCurrent = state.currentChapterIndex !== null && chapterSeq === state.currentChapterIndex;
      const chapterCls = isCurrent ? "outline-chapter is-current" : "outline-chapter";
      chunks.push(
        `<div class="${chapterCls}" data-chapter-index="${chapterSeq}"><strong>${escapeHtml(
          c.title || `第${ci + 1}章`
        )}</strong></div>`
      );
      chunks.push("<ul>");
      (c.points || []).forEach((p) => chunks.push(`<li>${escapeHtml(p)}</li>`));
      chunks.push("</ul>");
      chapterSeq += 1;
    });
    chunks.push("</details>");
  });
  el.outlineView.innerHTML = chunks.join("");
}

function scrollToOutlineChapter(index) {
  if (!el.outlineView) return;
  const target = el.outlineView.querySelector(`.outline-chapter[data-chapter-index="${Number(index)}"]`);
  if (!target) return;

  target.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
  target.classList.add("is-linked-focus");
  window.setTimeout(() => {
    target.classList.remove("is-linked-focus");
  }, 900);
}

function renderChapterList(chapters) {
  state.chapterMetas = Array.isArray(chapters) ? chapters : [];
  el.chapterList.innerHTML = "";
  chapters.forEach((c) => {
    const li = document.createElement("li");
    li.setAttribute("data-index", String(c.index));
    if (Number(state.selectedChapterIndex) === Number(c.index)) {
      li.classList.add("is-selected");
    }
    const btn = document.createElement("button");
    btn.textContent = `第${c.index + 1}章：${c.title || "未命名"}（${c.word_count || 0}字）`;
    btn.onclick = () => openChapter(c.index);
    li.appendChild(btn);
    el.chapterList.appendChild(li);
  });
  updateChapterActionButtons();
}

function highlightSelectedChapterInList(index) {
  if (!el.chapterList) return;
  const items = el.chapterList.querySelectorAll("li[data-index]");
  let selectedItem = null;
  items.forEach((li) => {
    const liIndex = Number(li.getAttribute("data-index"));
    const isSelected = liIndex === Number(index);
    li.classList.toggle("is-selected", isSelected);
    if (isSelected) selectedItem = li;
  });
  if (selectedItem && typeof selectedItem.scrollIntoView === "function") {
    selectedItem.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
  }
}

async function openChapter(index) {
  if (!state.currentProjectId) return;
  const projectIdAtRequest = state.currentProjectId;
  const requestedIndex = index;
  const reqSeq = (state.openChapterRequestSeq += 1);

  state.selectedChapterIndex = index;
  updateChapterActionButtons();
  highlightSelectedChapterInList(index);
  scrollToOutlineChapter(index);
  setStatus(`加载第 ${index + 1} 章...`);
  try {
    const data = await api.get(`/projects/${projectIdAtRequest}/chapters/${requestedIndex}`);
    // 丢弃旧请求：防止用户快速切换/后台生成结束后返回覆盖当前展示
    if (reqSeq !== state.openChapterRequestSeq) return;
    if (state.currentProjectId !== projectIdAtRequest) return;
    if (state.selectedChapterIndex !== requestedIndex) return;

    el.chapterView.innerHTML = renderMarkdown(data.content || "");
    adjustChapterViewHeight();
    setStatus(`已加载第 ${requestedIndex + 1} 章`);
  } catch (e) {
    if (reqSeq !== state.openChapterRequestSeq) return;
    setStatus(`加载章节失败：${e.message}`);
  }
}

function openChapterModal() {
  if (!el.chapterModal || !el.chapterModalContent) return;
  const chapterHtml = el.chapterView ? el.chapterView.innerHTML.trim() : "";
  if (!chapterHtml) {
    setStatus("请先在章节列表中打开一个章节");
    return;
  }
  el.chapterModalContent.innerHTML = chapterHtml;
  el.chapterModal.hidden = false;
}

function closeChapterModal() {
  if (!el.chapterModal) return;
  el.chapterModal.hidden = true;
}

async function openCharacterGraphModal() {
  if (!el.characterGraphModal || !el.characterGraphModalContent) return;
  if (!state.currentProjectId || state.selectedChapterIndex === null) {
    setStatus("请先在章节列表中打开一个章节");
    return;
  }
  const chapterIndex = Number(state.selectedChapterIndex);
  el.characterGraphModalContent.innerHTML = "<p>人物关系加载中...</p>";
  el.characterGraphModal.hidden = false;
  const data = await loadChapterCharacterGraph(chapterIndex);
  if (!data) {
    el.characterGraphModalContent.innerHTML = "<p>人物关系加载失败</p>";
    return;
  }
  el.characterGraphModalContent.innerHTML = buildChapterCharacterGraphHtml(data, chapterIndex);
}

function closeCharacterGraphModal() {
  if (!el.characterGraphModal) return;
  el.characterGraphModal.hidden = true;
}

/**
 * 生成/刷新剧情概要：已有当前项目时复用该项目，仅调用 plot-ideas；无项目时才 POST /projects 创建。
 * 结束后刷新列表与预览（loadProjects + openProject）。
 */
async function generateIdeas() {
  if (!el.instruction) {
    setStatus("错误：页面元素未加载完成，请刷新后重试");
    return;
  }
  const instruction = el.instruction.value.trim();
  if (!instruction) {
    setStatus("请先输入创作意图");
    return;
  }
  const btn = el.btnGenerateIdeas;
  const origText = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "处理中...";
  }
  if (el.btnGenerateOutline) {
    el.btnGenerateOutline.disabled = true;
  }
  try {
    if (!state.currentProjectId) {
      setStatus("创建项目...");
      const totalChapters = parseTotalChaptersInput();
      const p = await api.post("/projects", {
        instruction,
        ...(totalChapters ? { total_chapters: totalChapters } : {}),
      });
      const pid = p && p.project_id;
      if (!pid) {
        setStatus("操作失败：服务器未返回项目 ID");
        return;
      }
      state.currentProjectId = pid;
    }
    state.selectedIdea = "";
    state.expandedIdeaIndex = null;
    setStatus("生成概要中...");
    const ideas = await postNdjsonStream(
      `/projects/${state.currentProjectId}/plot-ideas`,
      { instruction },
      applyProgressToStatus
    );
    renderPlotIdeas(ideas.plot_ideas || []);
    await loadProjects();
    await openProject(state.currentProjectId);
    setStatus(`概要已生成（项目 ${state.currentProjectId}）`);
  } catch (e) {
    setStatus(`操作失败：${e.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = origText;
    }
    if (el.btnGenerateOutline) {
      el.btnGenerateOutline.disabled = state.isGeneratingOutline;
    }
  }
}

async function generateOutline() {
  if (state.isGeneratingOutline) {
    setStatus("大纲生成中，请稍候...");
    return;
  }
  if (!state.currentProjectId) {
    setStatus("请先创建或打开项目");
    return;
  }
  const custom = el.customSummary.value.trim();
  const selected = custom || state.selectedIdea;
  if (!selected) {
    setStatus("请先选择一条剧情概要，或填写自定义概要");
    return;
  }
  setStatus("生成大纲中...");
  setOutlineGeneratingUI(true);
  try {
    const totalChapters = parseTotalChaptersInput();
    const data = await postNdjsonStream(
      `/projects/${state.currentProjectId}/outline`,
      {
        selected_plot_summary: selected,
        ...(totalChapters ? { total_chapters: totalChapters } : {}),
      },
      applyProgressToStatus
    );
    renderOutline(data.outline_structure);
    await openProject(state.currentProjectId);
    setStatus("大纲生成完成");
  } catch (e) {
    setStatus(`生成大纲失败：${e.message}`);
  } finally {
    setOutlineGeneratingUI(false);
  }
}

async function writeNextChapter() {
  if (!state.currentProjectId) {
    setStatus("请先创建或打开项目");
    return;
  }
  setStatus("续写下一章中...");
  state.isChapterWriteInProgress = true;
  updateChapterActionButtons();
  setChapterNavigationLocked(true);
  setGlobalInteractionLocked(true);
  try {
    const { onProgress, flushNow } = createChapterStreamProgressHandler("refine_chapter_stream");
    if (el.chapterView) {
      el.chapterView.innerHTML = `<pre class="streaming-pre"></pre>`;
    }
    const data = await postNdjsonStream(
      `/projects/${state.currentProjectId}/chapters/next`,
      {},
      onProgress
    );
    flushNow();
    await openProject(state.currentProjectId);
    await openChapter(data.chapter_index);
    setStatus(`第 ${data.chapter_index + 1} 章已生成`);
  } catch (e) {
    setStatus(`续写失败：${e.message}`);
  } finally {
    state.isChapterWriteInProgress = false;
    updateChapterActionButtons();
    setChapterNavigationLocked(false);
    setGlobalInteractionLocked(false);
  }
}

async function rollbackTailFromSelectedChapter() {
  if (!state.currentProjectId) {
    setStatus("请先创建或打开项目");
    return;
  }
  if (state.selectedChapterIndex === null) {
    setStatus("请先在章节列表中打开一个章节");
    return;
  }
  const chapterNo = Number(state.selectedChapterIndex) + 1;
  const confirmed = window.confirm(
    `将保留第 ${chapterNo} 章及之前内容，并删除该章之后所有章节。此操作不可恢复，是否继续？`
  );
  if (!confirmed) return;

  setStatus(`回滚到第 ${chapterNo} 章中...`);
  state.isChapterWriteInProgress = true;
  updateChapterActionButtons();
  setChapterNavigationLocked(true);
  setGlobalInteractionLocked(true);
  try {
    const data = await api.delete(
      `/projects/${state.currentProjectId}/chapters/${state.selectedChapterIndex}/tail`
    );
    await openProject(state.currentProjectId);
    await openChapter(Number(data.kept_until));
    setStatus(`回滚完成：已删除 ${data.deleted_count} 章后续内容`);
  } catch (e) {
    setStatus(`回滚失败：${e.message}`);
  } finally {
    state.isChapterWriteInProgress = false;
    updateChapterActionButtons();
    setChapterNavigationLocked(false);
    setGlobalInteractionLocked(false);
  }
}

async function rewriteChapter() {
  if (!state.currentProjectId) {
    setStatus("请先创建或打开项目");
    return;
  }
  if (state.selectedChapterIndex === null) {
    setStatus("请先在章节列表中打开一个章节");
    return;
  }
  const feedback = el.feedback.value.trim();
  if (!feedback) {
    setStatus("请输入反馈内容");
    return;
  }
  setStatus("重写中...");
  state.isChapterWriteInProgress = true;
  updateChapterActionButtons();
  setChapterNavigationLocked(true);
  setGlobalInteractionLocked(true);
  try {
    const { onProgress, flushNow } = createChapterStreamProgressHandler("rewrite_feedback_stream");
    if (el.chapterView) {
      el.chapterView.innerHTML = `<pre class="streaming-pre"></pre>`;
    }
    await postNdjsonStream(
      `/projects/${state.currentProjectId}/chapters/${state.selectedChapterIndex}/rewrite`,
      {
        user_feedback: feedback,
        update_outline: el.updateOutline.checked,
      },
      onProgress
    );
    flushNow();
    await openProject(state.currentProjectId);
    await openChapter(state.selectedChapterIndex);
    setStatus("重写完成");
  } catch (e) {
    setStatus(`重写失败：${e.message}`);
  } finally {
    state.isChapterWriteInProgress = false;
    updateChapterActionButtons();
    setChapterNavigationLocked(false);
    setGlobalInteractionLocked(false);
  }
}

async function regenerateCurrentChapter() {
  if (!state.currentProjectId) {
    setStatus("请先创建或打开项目");
    return;
  }
  if (state.selectedChapterIndex === null) {
    setStatus("请先在章节列表中打开一个章节");
    return;
  }
  const chapterNo = Number(state.selectedChapterIndex) + 1;
  const confirmed = window.confirm(
    `将基于大纲重新生成第 ${chapterNo} 章，并覆盖当前正文。是否继续？`
  );
  if (!confirmed) return;

  setStatus(`重新生成第 ${chapterNo} 章中...`);
  state.isChapterWriteInProgress = true;
  updateChapterActionButtons();
  setChapterNavigationLocked(true);
  setGlobalInteractionLocked(true);
  try {
    const { onProgress, flushNow } = createChapterStreamProgressHandler("refine_chapter_stream");
    if (el.chapterView) {
      el.chapterView.innerHTML = `<pre class="streaming-pre"></pre>`;
    }
    const data = await postNdjsonStream(
      `/projects/${state.currentProjectId}/chapters/${state.selectedChapterIndex}/regenerate`,
      {},
      onProgress
    );
    flushNow();
    await openProject(state.currentProjectId);
    await openChapter(data.chapter_index);
    setStatus(`第 ${data.chapter_index + 1} 章已重新生成`);
  } catch (e) {
    setStatus(`重新生成失败：${e.message}`);
  } finally {
    state.isChapterWriteInProgress = false;
    updateChapterActionButtons();
    setChapterNavigationLocked(false);
    setGlobalInteractionLocked(false);
  }
}

function bindEvents() {
  const bindClick = (node, handler) => {
    if (!node || typeof handler !== "function") return;
    node.addEventListener("click", (event) => {
      event.preventDefault();
      Promise.resolve(handler(event)).catch((err) => {
        const msg = err instanceof Error ? err.message : String(err);
        setStatus(`操作失败：${msg}`);
      });
    });
  };

  bindClick(el.btnRefreshProjects, refreshProjectsAndHideDetail);
  bindClick(el.btnNewProject, startNewProject);
  bindClick(el.btnGenerateIdeas, generateIdeas);
  bindClick(el.btnGenerateOutline, generateOutline);
  bindClick(el.btnNextChapter, writeNextChapter);
  bindClick(el.btnRollbackTail, rollbackTailFromSelectedChapter);
  bindClick(el.btnRegenerateChapter, regenerateCurrentChapter);
  bindClick(el.btnRewrite, rewriteChapter);
  bindClick(el.btnViewChapterModal, openChapterModal);
  bindClick(el.btnViewCharacterGraphModal, openCharacterGraphModal);
  bindClick(el.btnCloseChapterModal, closeChapterModal);
  bindClick(el.btnCloseCharacterGraphModal, closeCharacterGraphModal);

  // 防御式兜底：即使未来引入了 form，也避免默认提交触发整页刷新。
  document.addEventListener("submit", (event) => {
    event.preventDefault();
  });

  if (el.chapterModal) {
    el.chapterModal.addEventListener("click", (event) => {
      if (event.target && event.target.getAttribute("data-close") === "true") {
        closeChapterModal();
      }
    });
  }
  if (el.characterGraphModal) {
    el.characterGraphModal.addEventListener("click", (event) => {
      if (event.target && event.target.getAttribute("data-close-graph") === "true") {
        closeCharacterGraphModal();
      }
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeChapterModal();
      closeCharacterGraphModal();
    }
  });
  window.addEventListener("resize", () => {
    adjustChapterViewHeight();
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", function () {
    setPlotIdeasSectionVisibility(false);
    bindEvents();
    loadProjects();
    adjustChapterViewHeight();
  });
} else {
  setPlotIdeasSectionVisibility(false);
  bindEvents();
  loadProjects();
  adjustChapterViewHeight();
}
