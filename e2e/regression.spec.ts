import { test, expect, type Page, type APIRequestContext } from "@playwright/test";

async function findProjectWithMinChapters(request: APIRequestContext, minChapters: number) {
  const projectsResp = await request.get("/projects");
  expect(projectsResp.ok()).toBeTruthy();
  const projectsJson = (await projectsResp.json()) as { projects?: Array<{ project_id: string }> };
  const projectIds = (projectsJson.projects || []).map((p) => p.project_id).filter(Boolean);

  for (const projectId of projectIds) {
    const pResp = await request.get(`/projects/${projectId}`);
    if (!pResp.ok()) continue;
    const pJson = (await pResp.json()) as { chapters?: Array<{ index: number }> };
    const chapters = Array.isArray(pJson.chapters) ? pJson.chapters : [];
    if (chapters.length >= minChapters) {
      return { projectId, chaptersCount: chapters.length };
    }
  }

  return null;
}

async function openProjectFromList(page: Page, projectId: string) {
  await page.goto("/");
  await expect(page.locator("#project-list")).toBeVisible();
  await page.getByRole("button", { name: `打开 ${projectId}` }).click();
  await expect(page.locator("#global-status")).toContainText(`已打开 ${projectId}`);
}

async function findProjectsWithMinChapters(
  request: APIRequestContext,
  minChapters: number,
  count: number
): Promise<Array<{ projectId: string; chaptersCount: number }>> {
  const projectsResp = await request.get("/projects");
  expect(projectsResp.ok()).toBeTruthy();
  const projectsJson = (await projectsResp.json()) as { projects?: Array<{ project_id: string }> };
  const projectIds = (projectsJson.projects || []).map((p) => p.project_id).filter(Boolean);

  const picked: Array<{ projectId: string; chaptersCount: number }> = [];
  for (const projectId of projectIds) {
    const pResp = await request.get(`/projects/${projectId}`);
    if (!pResp.ok()) continue;
    const pJson = (await pResp.json()) as { chapters?: Array<{ index: number }> };
    const chapters = Array.isArray(pJson.chapters) ? pJson.chapters : [];
    if (chapters.length >= minChapters) {
      picked.push({ projectId, chaptersCount: chapters.length });
      if (picked.length >= count) return picked;
    }
  }
  return picked;
}

test.describe("生成概要 / 项目创建交互", () => {
  test("第二次点「生成概要」复用当前项目：仅一次 POST /projects（stub，不调用 LLM）", async ({ page }) => {
    let postProjectsCount = 0;
    let postPlotIdeasCount = 0;

    const projectDetail = {
      project_id: "p-stub-reuse",
      instruction: "第二次意图",
      selected_plot_summary: "",
      outline_structure: { volumes: [] },
      chapters: [],
      current_chapter_index: 0,
      total_chapters: 100,
      chapter_word_target: 3000,
      enable_chapter_illustrations: false,
      created_at: 1700000000,
      token_usage: {},
    };

    await page.route("**/projects**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const path = url.pathname.replace(/\/$/, "") || "/";
      const method = req.method();

      if (method === "GET" && path === "/projects") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            projects: [{ project_id: "p-stub-reuse", created_at: projectDetail.created_at }],
          }),
        });
        return;
      }
      if (method === "GET" && path === "/projects/p-stub-reuse") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(projectDetail),
        });
        return;
      }
      if (method === "POST" && path === "/projects") {
        postProjectsCount += 1;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ project_id: "p-stub-reuse" }),
        });
        return;
      }
      if (method === "POST" && path.endsWith("/plot-ideas")) {
        postPlotIdeasCount += 1;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ plot_ideas: ["stub-idea-a", "stub-idea-b"] }),
        });
        return;
      }

      await route.continue();
    });

    await page.goto("/");
    await expect(page.locator("#instruction-input")).toBeVisible();
    await page.locator("#instruction-input").fill("第一次意图");
    await page.getByRole("button", { name: "生成概要" }).click();
    await expect(page.locator("#global-status")).toContainText("概要已生成", { timeout: 15_000 });

    await page.locator("#instruction-input").fill("第二次意图");
    await page.getByRole("button", { name: "生成概要" }).click();
    await expect(page.locator("#global-status")).toContainText("概要已生成", { timeout: 15_000 });

    expect(postProjectsCount, "POST /projects 应只发生一次").toBe(1);
    expect(postPlotIdeasCount, "POST .../plot-ideas 应发生两次").toBe(2);
  });

  test("生成概要请求未返回期间：「生成大纲」应保持 disabled（延迟 plot-ideas stub）", async ({ page }) => {
    const projectDetail = {
      project_id: "p-stub-outline-lock",
      instruction: "stub",
      selected_plot_summary: "",
      outline_structure: { volumes: [] },
      chapters: [],
      current_chapter_index: 0,
      total_chapters: 100,
      chapter_word_target: 3000,
      enable_chapter_illustrations: false,
      created_at: 1700000000,
      token_usage: {},
    };

    await page.route("**/projects**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const path = url.pathname.replace(/\/$/, "") || "/";
      const method = req.method();

      if (method === "GET" && path === "/projects") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            projects: [{ project_id: "p-stub-outline-lock", created_at: projectDetail.created_at }],
          }),
        });
        return;
      }
      if (method === "GET" && path === "/projects/p-stub-outline-lock") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(projectDetail),
        });
        return;
      }
      if (method === "POST" && path === "/projects") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ project_id: "p-stub-outline-lock" }),
        });
        return;
      }
      if (method === "POST" && path.endsWith("/plot-ideas")) {
        await new Promise((r) => setTimeout(r, 2000));
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ plot_ideas: ["stub-idea"] }),
        });
        return;
      }

      await route.continue();
    });

    await page.goto("/");
    await expect(page.locator("#instruction-input")).toBeVisible();
    const outlineBtn = page.locator("#btn-generate-outline");
    await expect(outlineBtn).toBeEnabled();

    await page.locator("#instruction-input").fill("测试意图");
    await page.getByRole("button", { name: "生成概要" }).click();

    await expect(outlineBtn).toBeDisabled();

    await expect(page.locator("#global-status")).toContainText("概要已生成", { timeout: 15_000 });
    await expect(outlineBtn).toBeEnabled();
  });
});

test.describe("UI regressions from past chats", () => {
  test("创建AI视频失败时：状态栏应展示后端失败原因（stub 视频任务失败）", async ({ page }) => {
    const pid = "p-video-fail-feedback";
    const projectDetail = {
      project_id: pid,
      instruction: "stub",
      selected_plot_summary: "stub",
      outline_structure: {
        volumes: [{ volume_title: "卷一", chapters: [{ title: "第一章", points: ["a"] }] }],
      },
      chapters: [{ index: 0, title: "第一章", word_count: 1000 }],
      current_chapter_index: 0,
      total_chapters: 1,
      chapter_word_target: 3000,
      enable_chapter_illustrations: false,
      created_at: 1700000000,
      token_usage: {},
      chapter_video_outputs: {},
    };

    await page.route("**/projects**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const path = url.pathname.replace(/\/$/, "") || "/";
      const method = req.method();

      if (method === "GET" && path === "/projects") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ projects: [{ project_id: pid, created_at: projectDetail.created_at }] }),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${pid}`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(projectDetail),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${pid}/chapters/0`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ index: 0, content: "# 第一章\n\n正文", meta: projectDetail.chapters[0] }),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${pid}/videos/chapters/0`) {
        await route.fulfill({
          status: 404,
          contentType: "application/json",
          body: JSON.stringify({ detail: "chapter video not found" }),
        });
        return;
      }
      if (method === "POST" && path === `/projects/${pid}/videos/chapters/0`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ job_id: "vj-fail-001", status: "pending", project_id: pid, chapter_index: 0 }),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${pid}/videos/jobs/vj-fail-001`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            job_id: "vj-fail-001",
            project_id: pid,
            chapter_index: 0,
            status: "failed",
            last_stage: "video_shot",
            last_message: "model 未配置",
          }),
        });
        return;
      }

      await route.continue();
    });

    await page.goto("/");
    await page.getByRole("button", { name: `打开 ${pid}` }).click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${pid}`);
    await page.getByRole("button", { name: /^第1章：/ }).click();

    await expect(page.locator("#btn-create-ai-video")).toBeVisible();
    await page.locator("#btn-create-ai-video").click();

    await expect(page.locator("#global-status")).toContainText("model 未配置");
    await expect(page.locator("#global-status")).toContainText("失败");
  });

  test("选择非最新章节时：隐藏续写/重写/重生成，显示回滚；选择最新章节时：隐藏回滚", async ({ page, request }) => {
    const picked = await findProjectWithMinChapters(request, 2);
    test.skip(!picked, "没有找到至少 2 章的项目，无法做章节选择类回归测试");

    const { projectId, chaptersCount } = picked!;
    await openProjectFromList(page, projectId);

    // 打开第 1 章（index=0），此时一定不是最新章（因为至少 2 章）
    await page.getByRole("button", { name: /^第1章：/ }).click();

    await expect(page.locator("#btn-next-chapter")).toBeHidden();
    await expect(page.locator("#btn-regenerate-chapter")).toBeHidden();
    await expect(page.locator("#btn-rewrite")).toBeHidden();

    await expect(page.locator("#btn-rollback-tail")).toBeVisible();

    // 再打开最新章节，回滚按钮应隐藏，其它写作按钮应出现
    await page.getByRole("button", { name: new RegExp(`^第${chaptersCount}章：`) }).click();

    await expect(page.locator("#btn-rollback-tail")).toBeHidden();
    await expect(page.locator("#btn-next-chapter")).toBeVisible();
  });

  test("续写请求未返回期间：三个按钮必须保持 disabled（通过延迟网络请求模拟）", async ({ page, request }) => {
    const picked = await findProjectWithMinChapters(request, 1);
    test.skip(!picked, "没有找到至少 1 章的项目，无法定位到最新章节执行续写按钮回归测试");

    const { projectId } = picked!;
    await openProjectFromList(page, projectId);

    // 确保处于最新章节：打开最后一章
    const chaptersResp = await request.get(`/projects/${projectId}/chapters`);
    expect(chaptersResp.ok()).toBeTruthy();
    const chaptersJson = (await chaptersResp.json()) as { chapters?: Array<{ index: number }> };
    const indices = (chaptersJson.chapters || []).map((c) => Number(c.index)).filter((n) => Number.isFinite(n));
    const latest = indices.length ? Math.max(...indices) : null;
    test.skip(latest === null, "项目没有章节 index，无法执行该回归测试");
    await page.getByRole("button", { name: new RegExp(`^第${latest + 1}章：`) }).click();

    // 延迟 /chapters/next 响应，验证在 pending 期间按钮是 disabled
    // 实际请求会附带 `?stream=1`，因此路由匹配需要放宽到可选 query
    const nextChapterPattern = new RegExp(`/projects/${projectId}/chapters/next(?:\\?.*)?$`);
    await page.route(nextChapterPattern, async (route) => {
      await new Promise((r) => setTimeout(r, 1500));
      await route.abort();
    });

    const nextBtn = page.locator("#btn-next-chapter");
    const regenBtn = page.locator("#btn-regenerate-chapter");
    const rewriteBtn = page.locator("#btn-rewrite");

    await expect(nextBtn).toBeVisible();
    await nextBtn.click();

    await expect(nextBtn).toBeDisabled();
    await expect(regenBtn).toBeDisabled();
    await expect(rewriteBtn).toBeDisabled();

    // 请求被 abort 后，页面应进入失败状态并恢复按钮状态（不强制检查文案）
    await expect(page.locator("#global-status")).toContainText("失败");
  });

  test("选择非最新章节时：反馈重写区块（含同时更新大纲）应隐藏", async ({ page, request }) => {
    const picked = await findProjectWithMinChapters(request, 2);
    test.skip(!picked, "没有找到至少 2 章的项目");
    const { projectId } = picked!;
    await openProjectFromList(page, projectId);
    await page.getByRole("button", { name: /^第1章：/ }).click();
    await expect(page.locator("#feedback-rewrite-section")).toBeHidden();
    await expect(page.locator("#update-outline-checkbox")).toBeHidden();
  });

  test("续写接口返回 5xx 时：页面不整页刷新，状态栏显示错误", async ({ page, request }) => {
    const picked = await findProjectWithMinChapters(request, 1);
    test.skip(!picked, "没有找到至少 1 章的项目");
    const { projectId } = picked!;
    await openProjectFromList(page, projectId);
    const chaptersResp = await request.get(`/projects/${projectId}/chapters`);
    const chaptersJson = (await chaptersResp.json()) as { chapters?: Array<{ index: number }> };
    const indices = (chaptersJson.chapters || []).map((c) => Number(c.index)).filter((n) => Number.isFinite(n));
    const latest = indices.length ? Math.max(...indices) : null;
    test.skip(latest === null, "项目没有章节");
    await page.getByRole("button", { name: new RegExp(`^第${latest + 1}章：`) }).click();

    // 实际请求会附带 `?stream=1`
    const nextUrl = new RegExp(`/projects/${projectId}/chapters/next(?:\\?.*)?$`);
    await page.route(nextUrl, (route) => route.fulfill({ status: 500, body: "Internal Server Error" }));

    const urlBefore = page.url();
    await page.locator("#btn-next-chapter").click();
    await expect(page.locator("#global-status")).toContainText(/失败|错误/, { timeout: 15000 });
    expect(page.url()).toBe(urlBefore);
  });

  test("切换项目时：右侧项目详情、大纲、章节列表刷新为选中项目", async ({ page, request }) => {
    const projectsResp = await request.get("/projects");
    const projectsJson = (await projectsResp.json()) as { projects?: Array<{ project_id: string }> };
    const ids = (projectsJson.projects || []).map((p) => p.project_id).filter(Boolean);
    test.skip(ids.length < 2, "至少需要 2 个项目才能测试切换");
    const [idA, idB] = ids.slice(0, 2);

    await openProjectFromList(page, idA);
    await expect(page.locator("#project-meta")).toContainText(idA);

    await page.getByRole("button", { name: `打开 ${idB}` }).click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${idB}`);
    await expect(page.locator("#project-meta")).toContainText(idB);
  });

  test("切换项目时：反馈输入与同时更新大纲应重置", async ({ page, request }) => {
    const picked = await findProjectsWithMinChapters(request, 1, 2);
    test.skip(picked.length < 2, "至少需要 2 个项目且每个项目至少 1 章");
    const [a, b] = picked;

    await openProjectFromList(page, a.projectId);
    await expect(page.locator("#feedback-rewrite-section")).toBeVisible();

    await page.locator("#feedback-input").fill("这是反馈 A");
    const updateOutline = page.locator("#update-outline-checkbox");
    await updateOutline.check();
    await expect(updateOutline).toBeChecked();

    await page.getByRole("button", { name: `打开 ${b.projectId}` }).click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${b.projectId}`);

    await expect(page.locator("#feedback-input")).toHaveValue("");
    await expect(page.locator("#update-outline-checkbox")).not.toBeChecked();
  });

  test("提交反馈重写后：反馈输入与同时更新大纲应重置（stub rewrite stream）", async ({
    page,
    request,
  }) => {
    const picked = await findProjectWithMinChapters(request, 1);
    test.skip(!picked, "没有找到至少 1 章的项目");
    const { projectId } = picked!;

    await openProjectFromList(page, projectId);
    await expect(page.locator("#btn-rewrite")).toBeVisible();
    await expect(page.locator("#feedback-rewrite-section")).toBeVisible();

    await page.locator("#feedback-input").fill("这是反馈 B");
    const updateOutline = page.locator("#update-outline-checkbox");
    await updateOutline.check();
    await expect(updateOutline).toBeChecked();

    const selectedLi = page.locator("#chapter-list li.is-selected");
    await expect(selectedLi).toBeVisible();
    const chapterIndexAttr = await selectedLi.getAttribute("data-index");
    test.skip(!chapterIndexAttr, "未能读取选中章节 index");
    const chapterIndex = Number(chapterIndexAttr);
    if (!Number.isFinite(chapterIndex)) test.skip(true, "章节 index 非数字");

    const rewriteUrlPattern = new RegExp(
      `/projects/${projectId}/chapters/${chapterIndex}/rewrite\\?stream=1`
    );
    const ndjson =
      `{"type":"progress","stage":"refine_chapter_stream","message":"处理中"}\n` +
      `{"type":"result","body":{"chapter_index":${chapterIndex}}}\n`;

    await page.route(rewriteUrlPattern, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/x-ndjson",
        body: ndjson,
      });
    });

    await page.locator("#btn-rewrite").click();
    await expect(page.locator("#global-status")).toContainText("重写完成", { timeout: 20000 });

    await expect(page.locator("#feedback-input")).toHaveValue("");
    await expect(page.locator("#update-outline-checkbox")).not.toBeChecked();
  });

  test("打开项目后：章节列表中选中项应滚动到可见（最新章节）", async ({ page, request }) => {
    const picked = await findProjectWithMinChapters(request, 3);
    test.skip(!picked, "需要至少 3 章以验证滚动");
    const { projectId, chaptersCount } = picked!;
    await openProjectFromList(page, projectId);
    const selectedLi = page.locator("#chapter-list li.is-selected");
    await expect(selectedLi).toBeVisible();
    await expect(selectedLi).toHaveAttribute("data-index", String(chaptersCount - 1));
  });

  test("项目列表按创建时间升序，且按钮展示创建时间（stub GET /projects）", async ({ page }) => {
    await page.route("**/projects", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          projects: [
            { project_id: "p-old", created_at: 1000 },
            { project_id: "p-mid", created_at: 2000 },
            { project_id: "p-new", created_at: 3000 },
          ],
        }),
      });
    });

    await page.goto("/");
    await expect(page.locator("#project-list")).toBeVisible();
    const buttons = page.locator(".project-open-btn");
    await expect(buttons).toHaveCount(3);
    await expect(buttons.nth(0)).toContainText("p-old");
    await expect(buttons.nth(1)).toContainText("p-mid");
    await expect(buttons.nth(2)).toContainText("p-new");
    await expect(buttons.nth(0)).toContainText(/\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}/);
  });

  test("长概要：选中项 project-preview 的 title 含全文（stub，不依赖 LLM）", async ({ page }) => {
    const longSummary = "叙".repeat(120);
    const projectDetail = {
      project_id: "p-long-preview",
      instruction: "",
      selected_plot_summary: longSummary,
      outline_structure: { volumes: [] },
      chapters: [],
      current_chapter_index: 0,
      total_chapters: 100,
      chapter_word_target: 3000,
      enable_chapter_illustrations: false,
      created_at: 1700000000,
      token_usage: {},
    };

    await page.route("**/projects**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const path = url.pathname.replace(/\/$/, "") || "/";
      const method = req.method();

      if (method === "GET" && path === "/projects") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            projects: [{ project_id: "p-long-preview", created_at: projectDetail.created_at }],
          }),
        });
        return;
      }
      if (method === "GET" && path === "/projects/p-long-preview") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(projectDetail),
        });
        return;
      }
      await route.continue();
    });

    await page.goto("/");
    await page.getByRole("button", { name: /打开 p-long-preview/ }).click();
    await expect(page.locator("#global-status")).toContainText("已打开 p-long-preview");

    const preview = page.locator(".project-preview");
    await expect(preview).toBeVisible();
    const fullExpected = `概要：${longSummary}`;
    await expect(preview).toHaveAttribute("title", fullExpected);
    const visibleLen = (await preview.innerText()).length;
    expect(visibleLen).toBeLessThan(fullExpected.length);
  });

  test("续写流式：多行 NDJSON 同包到达时预览须含尾部（节流/未 flush 会丢尾）", async ({ page }) => {
    const pid = "p-stream-tail-ui";
    const outline = {
      volumes: [{ volume_title: "卷1", chapters: [{ title: "第一章", points: ["stub"] }] }],
    };
    const chapter0 = { index: 0, title: "第一章", word_count: 3 };
    const chapter1 = { index: 1, title: "第二章", word_count: 3 };
    const projectV1 = {
      project_id: pid,
      instruction: "stub",
      selected_plot_summary: "stub",
      outline_structure: outline,
      chapters: [chapter0],
      current_chapter_index: 0,
      total_chapters: 10,
      chapter_word_target: 3000,
      enable_chapter_illustrations: false,
      created_at: 1700000000,
      token_usage: {},
    };
    const projectV2 = {
      ...projectV1,
      chapters: [chapter0, chapter1],
      current_chapter_index: 1,
    };

    let projectDetailGetCount = 0;
    const streamNdjson =
      '{"type":"progress","stage":"refine_chapter_stream","message":"STA"}\n' +
      '{"type":"progress","stage":"refine_chapter_stream","message":"RT_"}\n' +
      '{"type":"progress","stage":"refine_chapter_stream","message":"END_STREAM_TAIL"}\n' +
      '{"type":"progress","stage":"post_chapter","message":"写入中"}\n' +
      '{"type":"result","body":{"chapter_index":1,"chapter":"# 二\\n正文","meta":{}}}\n';

    await page.route("**/projects**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const path = url.pathname.replace(/\/$/, "") || "/";
      const method = req.method();

      if (method === "GET" && path === "/projects") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ projects: [{ project_id: pid, created_at: projectV1.created_at }] }),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${pid}`) {
        projectDetailGetCount += 1;
        if (projectDetailGetCount >= 2) {
          await new Promise((r) => setTimeout(r, 2500));
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(projectV2),
          });
          return;
        }
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(projectV1),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${pid}/chapters/0`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ index: 0, content: "# 一\n短", meta: {} }),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${pid}/chapters/1`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ index: 1, content: "# 二\n正文", meta: {} }),
        });
        return;
      }
      if (
        method === "POST" &&
        path === `/projects/${pid}/chapters/next` &&
        url.searchParams.get("stream") === "1"
      ) {
        await route.fulfill({
          status: 200,
          contentType: "application/x-ndjson",
          body: streamNdjson,
        });
        return;
      }

      await route.continue();
    });

    await page.goto("/");
    await expect(page.locator("#project-list")).toBeVisible();
    await page.getByRole("button", { name: `打开 ${pid}` }).click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${pid}`);

    await page.locator("#btn-next-chapter").click();

    const pre = page.locator("#chapter-view pre.streaming-pre");
    await expect(pre).toBeVisible({ timeout: 5000 });
    await expect(pre).toContainText("END_STREAM_TAIL");
    await expect(pre).toContainText("RT_");

    await expect(page.locator("#global-status")).toContainText("已生成", { timeout: 12_000 });
  });

  test("人物关系：点击按钮后弹窗展示（按当前章节）", async ({ page, request }) => {
    const picked = await findProjectWithMinChapters(request, 1);
    test.skip(!picked, "没有找到至少 1 章的项目");
    const { projectId } = picked!;
    await openProjectFromList(page, projectId);
    await page.getByRole("button", { name: /^第1章：/ }).click();
    await expect(page.locator("#btn-view-character-graph-modal")).toBeVisible();
    await page.getByRole("button", { name: "弹窗查看人物关系" }).click();
    await expect(page.locator("#character-graph-modal")).toBeVisible();
    await expect(page.locator("#character-graph-modal-content")).toBeVisible();
  });

  test("项目列表右键：应展示自定义菜单", async ({ page }) => {
    const pid = "p-ctx-menu";
    await page.route("**/projects", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          projects: [{ project_id: pid, nickname: null, created_at: 1000 }],
        }),
      });
    });

    await page.goto("/");
    const item = page.locator(".project-item").first();
    await expect(item).toBeVisible();
    await item.click({ button: "right" });
    await expect(page.locator("#project-context-menu")).toBeVisible();
    await expect(page.locator("#project-context-menu button[data-action='rename']")).toBeVisible();
    await expect(page.locator("#project-context-menu button[data-action='delete']")).toBeVisible();
  });

  test("项目重命名：右键后提交昵称，列表优先显示昵称", async ({ page }) => {
    const pid = "p-rename-target";
    let nickname = "";
    await page.route("**/projects**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const method = req.method();
      const path = url.pathname.replace(/\/$/, "") || "/";
      if (method === "GET" && path === "/projects") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            projects: [{ project_id: pid, nickname: nickname || null, created_at: 1000 }],
          }),
        });
        return;
      }
      if (method === "PATCH" && path === `/projects/${pid}`) {
        const body = req.postDataJSON() as { nickname?: string | null };
        nickname = String(body.nickname || "");
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ project_id: pid, nickname }),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${pid}`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            project_id: pid,
            nickname,
            instruction: "",
            selected_plot_summary: "",
            outline_structure: { volumes: [] },
            chapters: [],
            current_chapter_index: 0,
            total_chapters: 100,
            chapter_word_target: 3000,
            enable_chapter_illustrations: false,
            created_at: 1000,
            token_usage: {},
          }),
        });
        return;
      }
      await route.continue();
    });

    await page.goto("/");
    const item = page.locator(".project-item").first();
    await expect(item).toBeVisible();
    await item.click({ button: "right" });
    await page.locator("#project-context-menu button[data-action='rename']").click();
    await page.locator("#project-rename-input").fill("我的第一本书");
    await page.locator("#btn-project-rename-confirm").click();
    await expect(page.locator(".project-open-btn").first()).toContainText("我的第一本书");
    await expect(page.locator(".project-open-btn").first()).not.toContainText(pid);
  });

  test("项目删除：右键后确认删除，项目应从列表移除且刷新后仍不存在", async ({ page }) => {
    const pid = "p-delete-target";
    let deleted = false;
    await page.route("**/projects**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const method = req.method();
      const path = url.pathname.replace(/\/$/, "") || "/";
      if (method === "GET" && path === "/projects") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            projects: deleted ? [] : [{ project_id: pid, nickname: null, created_at: 1000 }],
          }),
        });
        return;
      }
      if (method === "DELETE" && path === `/projects/${pid}`) {
        deleted = true;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ project_id: pid, deleted: true }),
        });
        return;
      }
      await route.continue();
    });

    await page.goto("/");
    await expect(page.getByRole("button", { name: /打开 p-delete-target/ })).toBeVisible();
    await page.locator(".project-item").first().click({ button: "right" });
    await page.locator("#project-context-menu button[data-action='delete']").click();
    await expect(page.locator("#project-delete-modal")).toBeVisible();
    await page.locator("#btn-project-delete-confirm").click();
    await expect(page.getByRole("button", { name: /打开 p-delete-target/ })).toHaveCount(0);
    await page.locator("#btn-refresh-projects").click();
    await expect(page.getByRole("button", { name: /打开 p-delete-target/ })).toHaveCount(0);
  });
});

