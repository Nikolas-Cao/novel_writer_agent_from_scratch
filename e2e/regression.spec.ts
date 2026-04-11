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
  await page.locator(`.project-open-btn[data-project-id="${projectId}"]`).click();
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
  test("已有候选时再次点击生成概要：旧候选应先清空，再展示新候选", async ({ page }) => {
    let plotIdeasCallCount = 0;
    await page.route("**/projects**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const path = url.pathname.replace(/\/$/, "") || "/";
      const method = req.method();

      if (method === "GET" && path === "/projects") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ projects: [] }),
        });
        return;
      }
      if (method === "POST" && path === "/projects") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ project_id: "p-stub-regenerate-ideas" }),
        });
        return;
      }
      if (method === "POST" && path.endsWith("/plot-ideas")) {
        plotIdeasCallCount += 1;
        if (plotIdeasCallCount === 1) {
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({ plot_ideas: ["旧候选A", "旧候选B"] }),
          });
          return;
        }
        await new Promise((r) => setTimeout(r, 1200));
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ plot_ideas: ["新候选X"] }),
        });
        return;
      }

      await route.continue();
    });

    await page.goto("/");
    await page.locator("#instruction-input").fill("测试再次生成概要时清空旧候选");
    await page.getByRole("button", { name: "生成概要" }).click();
    await expect(page.locator("#plot-ideas .card")).toHaveCount(2);

    await page.getByRole("button", { name: "生成概要" }).click();
    await expect(page.locator("#plot-ideas .card")).toHaveCount(0);
    await expect(page.locator("#global-status")).toContainText("概要已生成", { timeout: 15000 });
    await expect(page.locator("#plot-ideas .card")).toHaveCount(1);
  });

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
      plot_ideas: ["stub-idea"],
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
    await expect(outlineBtn).toBeDisabled();

    await page.locator("#instruction-input").fill("测试意图");
    await page.getByRole("button", { name: "生成概要" }).click();

    await expect(outlineBtn).toBeDisabled();

    await expect(page.locator("#global-status")).toContainText("概要已生成", { timeout: 15_000 });
    await expect(outlineBtn).toBeEnabled();
  });
});

test.describe("文风约束弹窗与持久化", () => {
  test("可保存文风约束并在重新打开项目后回显", async ({ page }) => {
    const pid = "p-style-constraint";
    let savedStyleConstraint = "初始文风";
    let patchCount = 0;
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
            projects: [{ project_id: pid, created_at: 1700000000 }],
          }),
        });
        return;
      }

      if (method === "GET" && path === `/projects/${pid}`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            project_id: pid,
            instruction: "stub",
            style_constraint: savedStyleConstraint,
            selected_plot_summary: "stub",
            outline_structure: {
              volumes: [{ volume_title: "第一卷", chapters: [{ title: "第一章", points: ["p1"] }] }],
            },
            chapters: [],
            current_chapter_index: 0,
            total_chapters: 2,
            chapter_word_target: 1200,
            enable_chapter_illustrations: false,
            created_at: 1700000000,
            token_usage: {},
            selected_kb_ids: [],
            kb_enabled: false,
            canon_overrides: [],
          }),
        });
        return;
      }

      if (method === "PATCH" && path === `/projects/${pid}`) {
        patchCount += 1;
        const body = req.postDataJSON() as { style_constraint?: string };
        savedStyleConstraint = String(body.style_constraint || "");
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            project_id: pid,
            nickname: null,
            style_constraint: savedStyleConstraint,
          }),
        });
        return;
      }

      await route.continue();
    });

    await page.goto("/");
    await page.locator(`.project-open-btn[data-project-id="${pid}"]`).click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${pid}`);
    await expect(page.locator("#btn-style-constraint")).toBeVisible();

    await page.locator("#btn-style-constraint").click();
    await expect(page.locator("#style-constraint-modal")).toBeVisible();
    await page.locator("#style-constraint-input").fill("黑色电影感，句式短促。");
    await page.locator("#btn-style-constraint-save").click();
    await expect(page.locator("#global-status")).toContainText("文风约束已保存");
    expect(patchCount).toBe(1);

    await page.goto("/");
    await page.locator(`.project-open-btn[data-project-id="${pid}"]`).click();
    await page.locator("#btn-style-constraint").click();
    await expect(page.locator("#style-constraint-input")).toHaveValue("黑色电影感，句式短促。");
  });
});

test.describe("UI regressions from past chats", () => {
  test("窗口高度不足时：页面应可滚动并能操作生成大纲", async ({ page }) => {
    const pid = "p-small-viewport-outline";
    let created = false;
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
            projects: created ? [{ project_id: pid, created_at: 1700000000 }] : [],
          }),
        });
        return;
      }
      if (method === "POST" && path === "/projects") {
        created = true;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ project_id: pid }),
        });
        return;
      }
      if (method === "POST" && path === `/projects/${pid}/outline`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ outline_structure: { volumes: [] } }),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${pid}`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            project_id: pid,
            instruction: "测试小窗口滚动",
            selected_plot_summary: "可滚动时应能点到生成大纲",
            outline_structure: { volumes: [] },
            chapters: [],
            current_chapter_index: 0,
            total_chapters: 10,
            chapter_word_target: 3000,
            enable_chapter_illustrations: false,
            created_at: 1700000000,
            token_usage: {},
          }),
        });
        return;
      }
      await route.continue();
    });

    await page.setViewportSize({ width: 1280, height: 520 });
    await page.goto("/");
    await page.locator("#instruction-input").fill("测试小窗口滚动");
    await page.locator("#custom-summary-input").fill("可滚动时应能点到生成大纲");
    const outlineBtn = page.locator("#btn-generate-outline");
    await outlineBtn.scrollIntoViewIfNeeded();
    await expect(outlineBtn).toBeVisible();
    await outlineBtn.click();
    await expect(page.locator("#global-status")).toContainText("大纲生成完成", { timeout: 15000 });
  });

  test("窗口高度过小时：创作意图输入框应保持最小可读高度", async ({ page }) => {
    await page.route("**/projects", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ projects: [] }),
      });
    });
    await page.route("**/knowledge-bases", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ knowledge_bases: [] }),
      });
    });

    await page.setViewportSize({ width: 1280, height: 480 });
    await page.goto("/");
    await expect(page.locator("#instruction-input")).toBeVisible();
    const box = await page.locator("#instruction-input").boundingBox();
    expect(box).not.toBeNull();
    expect(box!.height).toBeGreaterThanOrEqual(110);
  });

  test("项目列表保持固定宽度，右侧区域随窗口宽度自适应", async ({ page }) => {
    await page.route("**/projects", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ projects: [] }),
      });
    });
    await page.route("**/knowledge-bases", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ knowledge_bases: [] }),
      });
    });

    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto("/");
    await expect(page.locator(".panel-list")).toBeVisible();
    await expect(page.locator(".panel-create")).toBeVisible();

    const leftNarrow = (await page.locator(".panel-list").boundingBox())?.width || 0;
    const rightNarrow = (await page.locator(".panel-create").boundingBox())?.width || 0;

    await page.setViewportSize({ width: 1600, height: 900 });
    await expect(page.locator(".panel-list")).toBeVisible();
    await expect(page.locator(".panel-create")).toBeVisible();

    const leftWide = (await page.locator(".panel-list").boundingBox())?.width || 0;
    const rightWide = (await page.locator(".panel-create").boundingBox())?.width || 0;

    expect(Math.abs(leftWide - leftNarrow)).toBeLessThanOrEqual(2);
    expect(rightWide).toBeGreaterThan(rightNarrow + 120);
  });

  test("大纲章节标题应显示连续编号前缀（1、2、3）", async ({ page }) => {
    const pid = "p-outline-numbering";
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
            projects: [{ project_id: pid, created_at: 1700000000 }],
          }),
        });
        return;
      }

      if (method === "GET" && path === `/projects/${pid}`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            project_id: pid,
            instruction: "stub",
            selected_plot_summary: "stub",
            outline_structure: {
              volumes: [
                {
                  volume_title: "第一卷",
                  chapters: [
                    { title: "系统觉醒", description: "d1", points: ["p1"] },
                    { title: "初战告捷", description: "d2", points: ["p2"] },
                  ],
                },
                {
                  volume_title: "第二卷",
                  chapters: [{ title: "暗流涌动", description: "d3", points: ["p3"] }],
                },
              ],
            },
            chapters: [],
            current_chapter_index: 0,
            total_chapters: 200,
            chapter_word_target: 3000,
            enable_chapter_illustrations: false,
            created_at: 1700000000,
            token_usage: {},
          }),
        });
        return;
      }

      await route.continue();
    });

    await page.goto("/");
    await page.locator(`.project-open-btn[data-project-id="${pid}"]`).click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${pid}`);

    const outlineTitles = page.locator("#outline-view .outline-chapter strong");
    await expect(outlineTitles).toHaveCount(3);
    await expect(outlineTitles.nth(0)).toHaveText("1、系统觉醒");
    await expect(outlineTitles.nth(1)).toHaveText("2、初战告捷");
    await expect(outlineTitles.nth(2)).toHaveText("3、暗流涌动");
  });

  test("刷新后重开项目：应按进度恢复候选概要；进入大纲阶段后候选区隐藏", async ({ page }) => {
    const ideasProjectId = "p-progress-ideas";
    const outlinedProjectId = "p-progress-outlined";
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
            projects: [
              { project_id: ideasProjectId, created_at: 1700000000 },
              { project_id: outlinedProjectId, created_at: 1700000001 },
            ],
          }),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${ideasProjectId}`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            project_id: ideasProjectId,
            instruction: "这是用于恢复的创作意图",
            plot_ideas: ["候选剧情A", "候选剧情B"],
            selected_plot_summary: "",
            outline_structure: { volumes: [] },
            chapters: [],
            current_chapter_index: 0,
            total_chapters: 10,
            chapter_word_target: 3000,
            enable_chapter_illustrations: false,
            created_at: 1700000000,
            token_usage: {},
            selected_kb_ids: [],
            kb_enabled: false,
            canon_overrides: [],
          }),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${outlinedProjectId}`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            project_id: outlinedProjectId,
            instruction: "已进入大纲阶段",
            plot_ideas: ["旧候选1", "旧候选2"],
            selected_plot_summary: "旧候选1",
            outline_structure: {
              volumes: [{ volume_title: "第一卷", chapters: [{ title: "第一章", points: ["p1"] }] }],
            },
            chapters: [],
            current_chapter_index: 0,
            total_chapters: 10,
            chapter_word_target: 3000,
            enable_chapter_illustrations: false,
            created_at: 1700000001,
            token_usage: {},
            selected_kb_ids: [],
            kb_enabled: false,
            canon_overrides: [],
          }),
        });
        return;
      }
      await route.continue();
    });

    await page.goto("/");
    await page.locator(`.project-open-btn[data-project-id="${ideasProjectId}"]`).click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${ideasProjectId}`);
    await expect(page.locator("#instruction-input")).toHaveValue("这是用于恢复的创作意图");
    await expect(page.locator("#plot-ideas-section")).toBeVisible();
    await expect(page.locator("#plot-ideas .card")).toHaveCount(2);

    await page.reload();
    await page.locator(`.project-open-btn[data-project-id="${ideasProjectId}"]`).click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${ideasProjectId}`);
    await expect(page.locator("#instruction-input")).toHaveValue("这是用于恢复的创作意图");
    await expect(page.locator("#plot-ideas-section")).toBeVisible();
    await expect(page.locator("#plot-ideas .card")).toHaveCount(2);

    await page.locator(`.project-open-btn[data-project-id="${outlinedProjectId}"]`).click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${outlinedProjectId}`);
    await expect(page.locator("#plot-ideas-section")).toBeHidden();
  });

  test("点击新建项目：应清空创作意图与目标章节等新建区输入", async ({ page }) => {
    const pid = "p-clear-on-new";
    await page.route("**/knowledge-bases**", async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ knowledge_bases: [] }),
        });
        return;
      }
      await route.continue();
    });
    await page.route("**/projects**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const method = req.method();
      const path = url.pathname.replace(/\/$/, "") || "/";
      if (method === "GET" && path === "/projects") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ projects: [{ project_id: pid, created_at: 1700000000 }] }),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${pid}`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            project_id: pid,
            instruction: "打开项目后残留的创作意图文案",
            plot_ideas: [],
            selected_plot_summary: "",
            outline_structure: { volumes: [] },
            chapters: [],
            current_chapter_index: 0,
            total_chapters: 200,
            chapter_word_target: 3000,
            enable_chapter_illustrations: false,
            created_at: 1700000000,
            token_usage: {},
            selected_kb_ids: [],
            kb_enabled: false,
            canon_overrides: [],
          }),
        });
        return;
      }
      await route.continue();
    });

    await page.goto("/");
    await page.locator(`.project-open-btn[data-project-id="${pid}"]`).click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${pid}`);
    await expect(page.locator("#instruction-input")).toHaveValue("打开项目后残留的创作意图文案");
    await expect(page.locator("#total-chapters-input")).toHaveValue("200");

    await page.locator("#btn-new-project").click();
    await expect(page.locator("#instruction-input")).toHaveValue("");
    await expect(page.locator("#total-chapters-input")).toHaveValue("");
    await expect(page.locator("#custom-summary-input")).toHaveValue("");
    await expect(page.locator("#create-kb-selection-hint")).toBeHidden();
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
    await expect(page.locator("#enable-chapter-illustrations-label")).toBeHidden();

    await expect(page.locator("#btn-rollback-tail")).toBeVisible();

    // 再打开最新章节，回滚按钮应隐藏，其它写作按钮应出现
    await page.getByRole("button", { name: new RegExp(`^第${chaptersCount}章：`) }).click();

    await expect(page.locator("#btn-rollback-tail")).toBeHidden();
    await expect(page.locator("#btn-next-chapter")).toBeVisible();
    await expect(page.locator("#enable-chapter-illustrations-label")).toBeVisible();
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

  test("选择非最新章节时：反馈重写按钮应隐藏", async ({ page, request }) => {
    const picked = await findProjectWithMinChapters(request, 2);
    test.skip(!picked, "没有找到至少 2 章的项目");
    const { projectId } = picked!;
    await openProjectFromList(page, projectId);
    await page.getByRole("button", { name: /^第1章：/ }).click();
    await expect(page.locator("#btn-rewrite")).toBeHidden();
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
    const projectAResp = await request.get(`/projects/${idA}`);
    const projectBResp = await request.get(`/projects/${idB}`);
    const projectA = projectAResp.ok() ? ((await projectAResp.json()) as { nickname?: string | null }) : {};
    const projectB = projectBResp.ok() ? ((await projectBResp.json()) as { nickname?: string | null }) : {};
    const expectedNameA = String(projectA.nickname || "").trim() || idA;
    const expectedNameB = String(projectB.nickname || "").trim() || idB;

    await openProjectFromList(page, idA);
    await expect(page.locator("#project-meta")).toContainText(expectedNameA);

    await page.locator(`.project-open-btn[data-project-id="${idB}"]`).click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${idB}`);
    await expect(page.locator("#project-meta")).toContainText(expectedNameB);
  });

  test("切换项目时：反馈重写弹窗输入与勾选应重置", async ({ page, request }) => {
    const picked = await findProjectsWithMinChapters(request, 1, 2);
    test.skip(picked.length < 2, "至少需要 2 个项目且每个项目至少 1 章");
    const [a, b] = picked;

    await openProjectFromList(page, a.projectId);
    await expect(page.locator("#btn-rewrite")).toBeVisible();
    await page.locator("#btn-rewrite").click();
    await expect(page.locator("#feedback-rewrite-modal")).toBeVisible();
    await page.locator("#feedback-modal-input").fill("这是反馈 A");
    const updateOutline = page.locator("#feedback-modal-update-outline-checkbox");
    await updateOutline.check();
    await expect(updateOutline).toBeChecked();

    await page.locator("#btn-feedback-rewrite-cancel").click();
    await expect(page.locator("#feedback-rewrite-modal")).toBeHidden();
    await page.locator(`.project-open-btn[data-project-id="${b.projectId}"]`).click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${b.projectId}`);
    await page.locator("#btn-rewrite").click();
    await expect(page.locator("#feedback-rewrite-modal")).toBeVisible();
    await expect(page.locator("#feedback-modal-input")).toHaveValue("");
    await expect(page.locator("#feedback-modal-update-outline-checkbox")).not.toBeChecked();
  });

  test("提交反馈重写后：弹窗关闭并重置输入与勾选（stub rewrite stream）", async ({
    page,
    request,
  }) => {
    const picked = await findProjectWithMinChapters(request, 1);
    test.skip(!picked, "没有找到至少 1 章的项目");
    const { projectId } = picked!;

    await openProjectFromList(page, projectId);
    await expect(page.locator("#btn-rewrite")).toBeVisible();
    await page.locator("#btn-rewrite").click();
    await expect(page.locator("#feedback-rewrite-modal")).toBeVisible();

    await page.locator("#feedback-modal-input").fill("这是反馈 B");
    const updateOutline = page.locator("#feedback-modal-update-outline-checkbox");
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
      const body = route.request().postDataJSON() as {
        user_feedback?: string;
        update_outline?: boolean;
      };
      expect(body.user_feedback).toBe("这是反馈 B");
      expect(body.update_outline).toBeTruthy();
      await route.fulfill({
        status: 200,
        contentType: "application/x-ndjson",
        body: ndjson,
      });
    });

    await page.locator("#btn-feedback-rewrite-confirm").click();
    await expect(page.locator("#global-status")).toContainText("重写完成", { timeout: 20000 });
    await expect(page.locator("#feedback-rewrite-modal")).toBeHidden();
    await page.locator("#btn-rewrite").click();
    await expect(page.locator("#feedback-modal-input")).toHaveValue("");
    await expect(page.locator("#feedback-modal-update-outline-checkbox")).not.toBeChecked();
  });

  test("反馈重写弹窗取消后：关闭弹窗且不触发 rewrite 请求", async ({ page, request }) => {
    const picked = await findProjectWithMinChapters(request, 1);
    test.skip(!picked, "没有找到至少 1 章的项目");
    const { projectId } = picked!;
    await openProjectFromList(page, projectId);
    await expect(page.locator("#btn-rewrite")).toBeVisible();

    let rewriteCalled = false;
    const rewriteUrlPattern = new RegExp(`/projects/${projectId}/chapters/\\d+/rewrite\\?stream=1`);
    await page.route(rewriteUrlPattern, async (route) => {
      rewriteCalled = true;
      await route.abort();
    });

    await page.locator("#btn-rewrite").click();
    await expect(page.locator("#feedback-rewrite-modal")).toBeVisible();
    await page.locator("#feedback-modal-input").fill("先不提交");
    await page.locator("#btn-feedback-rewrite-cancel").click();
    await expect(page.locator("#feedback-rewrite-modal")).toBeHidden();
    expect(rewriteCalled).toBeFalsy();
  });

  test("反馈重写：点击确认后应立即关闭弹窗（请求进行中）", async ({ page, request }) => {
    const picked = await findProjectWithMinChapters(request, 1);
    test.skip(!picked, "没有找到至少 1 章的项目");
    const { projectId } = picked!;
    await openProjectFromList(page, projectId);
    await expect(page.locator("#btn-rewrite")).toBeVisible();
    await page.locator("#btn-rewrite").click();
    await expect(page.locator("#feedback-rewrite-modal")).toBeVisible();
    await page.locator("#feedback-modal-input").fill("立即关闭弹窗测试");

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
      await new Promise((r) => setTimeout(r, 1200));
      await route.fulfill({
        status: 200,
        contentType: "application/x-ndjson",
        body: ndjson,
      });
    });

    await page.locator("#btn-feedback-rewrite-confirm").click();
    await expect(page.locator("#feedback-rewrite-modal")).toBeHidden({ timeout: 200 });
    await expect(page.locator("#global-status")).toContainText("重写完成", { timeout: 20000 });
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

  test("项目列表按创建时间升序，且按钮仅展示项目名（stub GET /projects）", async ({ page }) => {
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
    await expect(buttons.nth(0)).toHaveText("p-old");
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
    await page.locator(`.project-open-btn[data-project-id="p-long-preview"]`).click();
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
    await page.locator(`.project-open-btn[data-project-id="${pid}"]`).click();
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

  test("章节全文/人物关系弹窗：重复打开时滚动位置应重置到顶部", async ({ page }) => {
    const pid = "p-modal-scroll-reset";
    const longChapter = Array.from({ length: 240 }, (_, i) => `第${i + 1}行正文`).join("\n");
    const graphNodes = Array.from({ length: 120 }, (_, i) => ({
      id: `n${i + 1}`,
      name: `人物${i + 1}`,
      description: `描述${i + 1}`,
    }));
    const graphEdges = Array.from({ length: 80 }, (_, i) => ({
      from_id: `n${(i % 40) + 1}`,
      to_id: `n${(i % 40) + 41}`,
      relation: "关联",
      note: `关系${i + 1}`,
    }));

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
            projects: [{ project_id: pid, created_at: 1700000000 }],
          }),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${pid}`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            project_id: pid,
            instruction: "stub",
            selected_plot_summary: "stub",
            outline_structure: {
              volumes: [{ volume_title: "卷1", chapters: [{ title: "第一章", points: ["stub"] }] }],
            },
            chapters: [{ index: 0, title: "第一章", word_count: 5000 }],
            current_chapter_index: 0,
            total_chapters: 1,
            chapter_word_target: 3000,
            enable_chapter_illustrations: false,
            created_at: 1700000000,
            token_usage: {},
          }),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${pid}/chapters/0`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ index: 0, content: longChapter, meta: {} }),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${pid}/character-graph`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ character_graph: { nodes: graphNodes, edges: graphEdges } }),
        });
        return;
      }

      await route.continue();
    });

    await page.goto("/");
    await page.locator(`.project-open-btn[data-project-id="${pid}"]`).click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${pid}`);

    const chapterContent = page.locator("#chapter-modal-content");
    await page.locator("#btn-view-chapter-modal").click();
    await expect(page.locator("#chapter-modal")).toBeVisible();
    await chapterContent.evaluate((node) => {
      node.scrollTop = 600;
    });
    await expect(chapterContent.evaluate((node) => node.scrollTop)).resolves.toBeGreaterThan(0);
    await page.locator("#btn-close-chapter-modal").click();
    await expect(page.locator("#chapter-modal")).toBeHidden();
    await page.locator("#btn-view-chapter-modal").click();
    await expect(page.locator("#chapter-modal")).toBeVisible();
    await expect(chapterContent.evaluate((node) => node.scrollTop)).resolves.toBe(0);
    await page.locator("#btn-close-chapter-modal").click();

    const graphContent = page.locator("#character-graph-modal-content");
    await page.locator("#btn-view-character-graph-modal").click();
    await expect(page.locator("#character-graph-modal")).toBeVisible();
    await expect(graphContent.locator(".graph-meta")).toBeVisible();
    const graphCanScroll = await graphContent.evaluate((node) => node.scrollHeight > node.clientHeight + 2);
    if (graphCanScroll) {
      await graphContent.evaluate((node) => {
        node.scrollTop = 600;
      });
      await expect(graphContent.evaluate((node) => node.scrollTop)).resolves.toBeGreaterThan(0);
    }
    await page.locator("#btn-close-character-graph-modal").click();
    await expect(page.locator("#character-graph-modal")).toBeHidden();
    await page.locator("#btn-view-character-graph-modal").click();
    await expect(page.locator("#character-graph-modal")).toBeVisible();
    await expect(graphContent.evaluate((node) => node.scrollTop)).resolves.toBe(0);
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
    await expect(page.locator(`.project-open-btn[data-project-id="${pid}"]`)).toBeVisible();
    await page.locator(".project-item").first().click({ button: "right" });
    await page.locator("#project-context-menu button[data-action='delete']").click();
    await expect(page.locator("#project-delete-modal")).toBeVisible();
    await page.locator("#btn-project-delete-confirm").click();
    await expect(page.locator(`.project-open-btn[data-project-id="${pid}"]`)).toHaveCount(0);
    await page.locator("#btn-refresh-projects").click();
    await expect(page.locator(`.project-open-btn[data-project-id="${pid}"]`)).toHaveCount(0);
  });
});

test.describe("知识库入口弹窗化", () => {
  test("页面仅通过“知识库”按钮打开弹窗入口", async ({ page }) => {
    await page.route("**/projects", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ projects: [] }),
      });
    });
    await page.route("**/knowledge-bases", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ knowledge_bases: [] }),
      });
    });

    await page.goto("/");
    await expect(page.locator("#btn-open-kb-modal")).toBeVisible();
    await expect(page.locator("#kb-panel")).toHaveCount(0);
    await page.locator("#btn-open-kb-modal").click();
    await expect(page.locator("#knowledge-base-modal")).toBeVisible();
  });

  test("弹窗内可创建知识库并显示在列表", async ({ page }) => {
    const kbStore: Array<{ kb_id: string; name: string }> = [];

    await page.route("**/projects", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ projects: [{ project_id: "p-kb-1", nickname: "项目一", created_at: 1000 }] }),
      });
    });

    await page.route("**/knowledge-bases", async (route) => {
      const method = route.request().method();
      if (method === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ knowledge_bases: kbStore }),
        });
        return;
      }
      if (method === "POST") {
        const body = route.request().postDataJSON() as { name?: string };
        const name = String(body.name || "").trim();
        kbStore.push({ kb_id: `kb-${kbStore.length + 1}`, name });
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(kbStore[kbStore.length - 1]),
        });
        return;
      }
      await route.continue();
    });

    await page.route("**/knowledge-bases/*", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ documents: [] }),
      });
    });

    await page.goto("/");
    await page.locator("#btn-open-kb-modal").click();
    await expect(page.locator("#knowledge-base-modal")).toBeVisible();
    await page.locator("#kb-new-name-input").fill("武侠设定集");
    await page.locator("#btn-create-kb").click();
    await expect(page.locator("#global-status")).toContainText("知识集已创建");
    await expect(page.locator("#kb-list .kb-item")).toHaveCount(1);
    await expect(page.locator("#kb-list .kb-item strong")).toContainText("武侠设定集");
  });

  test("新建小说区通过按钮弹框选择参考知识库，并在意图下方展示已选提示", async ({ page }) => {
    await page.route("**/projects", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ projects: [] }),
      });
    });
    await page.route("**/knowledge-bases", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          knowledge_bases: [
            { kb_id: "kb-1", name: "世界观设定" },
            { kb_id: "kb-2", name: "角色档案" },
          ],
        }),
      });
    });

    await page.goto("/");
    await expect(page.locator("#create-kb-selection-hint")).toBeHidden();
    await page.locator("#btn-open-create-kb-picker").click();
    await expect(page.locator("#create-kb-picker-modal")).toBeVisible();
    await expect(page.locator(".create-kb-card[data-kb-id='kb-1']")).toBeVisible();
    await expect(page.locator(".create-kb-card[data-kb-id='kb-2']")).toBeVisible();

    await page.locator(".create-kb-card[data-kb-id='kb-1']").click();
    await page.locator(".create-kb-card[data-kb-id='kb-2']").click();
    await page.locator("#btn-confirm-create-kb-picker").click();

    await expect(page.locator("#create-kb-picker-modal")).toBeHidden();
    await expect(page.locator("#create-kb-selection-hint")).toBeVisible();
    await expect(page.locator("#create-kb-selection-hint")).toContainText("当前小说创作参考知识库：世界观设定，角色档案");
  });

  test("生成概要与生成大纲创建项目时携带知识库多选结果", async ({ page }) => {
    const createBodies: Array<any> = [];
    await page.route("**/projects**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const path = url.pathname.replace(/\/$/, "") || "/";
      const method = req.method();
      if (method === "GET" && path === "/projects") {
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ projects: [] }) });
        return;
      }
      if (method === "POST" && path === "/projects") {
        const body = req.postDataJSON() as any;
        createBodies.push(body);
        const pid = `p-kb-create-${createBodies.length}`;
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ project_id: pid }) });
        return;
      }
      if (method === "POST" && path.endsWith("/plot-ideas")) {
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ plot_ideas: ["stub"] }) });
        return;
      }
      if (method === "POST" && path.endsWith("/outline")) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ outline_structure: { volumes: [] } }),
        });
        return;
      }
      if (method === "GET" && /^\/projects\/p-kb-create-\d+$/.test(path)) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            project_id: path.split("/").pop(),
            instruction: "stub",
            selected_plot_summary: "",
            outline_structure: { volumes: [] },
            chapters: [],
            current_chapter_index: 0,
            total_chapters: 10,
            token_usage: {},
          }),
        });
        return;
      }
      await route.continue();
    });
    await page.route("**/knowledge-bases", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          knowledge_bases: [
            { kb_id: "kb-1", name: "世界观设定" },
            { kb_id: "kb-2", name: "角色档案" },
          ],
        }),
      });
    });

    await page.goto("/");
    await page.locator("#instruction-input").fill("测试意图");
    await page.locator("#btn-open-create-kb-picker").click();
    await page.locator(".create-kb-card[data-kb-id='kb-1']").click();
    await page.locator(".create-kb-card[data-kb-id='kb-2']").click();
    await page.locator("#btn-confirm-create-kb-picker").click();
    await page.locator("#btn-generate-ideas").click();
    await expect(page.locator("#global-status")).toContainText("概要已生成", { timeout: 15000 });
    expect(createBodies[0].selected_kb_ids).toEqual(["kb-1", "kb-2"]);

    await page.locator("#btn-new-project").click();
    await page.locator("#instruction-input").fill("测试大纲");
    await expect(page.locator("#create-kb-selection-hint")).toBeHidden();
    await page.locator("#custom-summary-input").fill("这是一条自定义概要");
    await page.locator("#btn-generate-outline").click();
    await expect(page.locator("#global-status")).toContainText("大纲生成完成", { timeout: 15000 });
    expect(Object.prototype.hasOwnProperty.call(createBodies[1], "selected_kb_ids")).toBeFalsy();
  });

  test("知识摘要弹窗可编辑并保存结构化 JSON", async ({ page }) => {
    let savedAssetsBody: any = null;
    const initialAssets = {
      global_summary: "初始摘要",
      characters: [{ name: "角色A", aliases: [], role: "主角", relations: "与角色B搭档" }],
      timeline: [{ order: 1, event: "事件一", actors: "角色A, 角色B" }],
      world_rules: [{ rule: "规则一", note: "备注一" }],
      core_facts: [{ fact: "事实一", importance: "high" }],
      leaf_summaries: [{ id: "leaf-1", summary: "leaf" }],
      section_summaries: [{ id: "section-1", summary: "section" }],
      by_doc: { d1: { status: "ready", global_summary: "自动摘要" } },
      status: "ready",
    };

    await page.route("**/projects", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ projects: [] }),
      });
    });

    await page.route("**/knowledge-bases", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ knowledge_bases: [{ kb_id: "kb-1", name: "知识集一" }] }),
      });
    });

    await page.route("**/knowledge-bases/kb-1/assets/summary", async (route) => {
      const method = route.request().method();
      if (method === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ kb_id: "kb-1", doc_id: null, assets: initialAssets }),
        });
        return;
      }
      if (method === "PUT") {
        savedAssetsBody = route.request().postDataJSON();
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ kb_id: "kb-1", saved: true, assets: savedAssetsBody.assets }),
        });
        return;
      }
      await route.continue();
    });

    await page.goto("/");
    await page.locator("#btn-open-kb-modal").click();
    await page.locator("#btn-load-kb-assets").click();
    await expect(page.locator("#kb-assets-modal")).toBeVisible();

    await expect(page.locator("#btn-edit-kb-assets")).toContainText("编辑");
    await page.locator("#btn-edit-kb-assets").click();
    await expect(page.locator("#btn-edit-kb-assets")).toContainText("保存");
    await expect(page.locator("#kb-assets-editor")).toBeVisible();

    const editor = page.locator("#kb-assets-editor");
    const editable = {
      global_summary: "人工修订摘要",
      characters: [{ name: "角色A", aliases: ["A"], role: "主角", relations: "与角色B搭档" }],
      timeline: [{ order: 1, event: "事件一", actors: "角色A, 角色B" }],
      world_rules: [{ rule: "规则一", note: "备注一" }],
      core_facts: [{ fact: "事实一", importance: "high" }],
      leaf_summaries: [{ id: "leaf-1", summary: "leaf" }],
      section_summaries: [{ id: "section-1", summary: "section" }],
    };
    await editor.fill(JSON.stringify(editable, null, 2));
    await page.locator("#btn-edit-kb-assets").click();

    await expect(page.locator("#global-status")).toContainText("知识摘要已保存");
    expect(savedAssetsBody).not.toBeNull();
    expect(savedAssetsBody.assets.global_summary).toBe("人工修订摘要");
    expect(savedAssetsBody.assets.by_doc).toBeUndefined();
    expect(savedAssetsBody.assets.status).toBeUndefined();
  });

  test("知识摘要保存中重复点击不会触发多次 PUT", async ({ page }) => {
    let putCount = 0;
    const initialAssets = {
      global_summary: "初始摘要",
      characters: [{ name: "角色A", aliases: [], role: "主角", relations: "与角色B搭档" }],
      timeline: [{ order: 1, event: "事件一", actors: "角色A, 角色B" }],
      world_rules: [{ rule: "规则一", note: "备注一" }],
      core_facts: [{ fact: "事实一", importance: "high" }],
      leaf_summaries: [{ id: "leaf-1", summary: "leaf" }],
      section_summaries: [{ id: "section-1", summary: "section" }],
      by_doc: {},
      status: "ready",
    };

    await page.route("**/projects", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ projects: [] }),
      });
    });

    await page.route("**/knowledge-bases", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ knowledge_bases: [{ kb_id: "kb-1", name: "知识集一" }] }),
      });
    });

    await page.route("**/knowledge-bases/kb-1/assets/summary", async (route) => {
      const method = route.request().method();
      if (method === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ kb_id: "kb-1", doc_id: null, assets: initialAssets }),
        });
        return;
      }
      if (method === "PUT") {
        putCount += 1;
        await new Promise((r) => setTimeout(r, 300));
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ kb_id: "kb-1", saved: true, assets: route.request().postDataJSON().assets }),
        });
        return;
      }
      await route.continue();
    });

    await page.goto("/");
    await page.locator("#btn-open-kb-modal").click();
    await page.locator("#btn-load-kb-assets").click();
    await page.locator("#btn-edit-kb-assets").click();
    await page.locator("#kb-assets-editor").fill(
      JSON.stringify(
        {
          global_summary: "人工修订摘要",
          characters: [{ name: "角色A", aliases: [], role: "主角", relations: "与角色B搭档" }],
          timeline: [{ order: 1, event: "事件一", actors: "角色A, 角色B" }],
          world_rules: [{ rule: "规则一", note: "备注一" }],
          core_facts: [{ fact: "事实一", importance: "high" }],
          leaf_summaries: [{ id: "leaf-1", summary: "leaf" }],
          section_summaries: [{ id: "section-1", summary: "section" }],
        },
        null,
        2
      )
    );

    const saveBtn = page.locator("#btn-edit-kb-assets");
    await page.evaluate(() => {
      const btn = document.querySelector("#btn-edit-kb-assets") as HTMLButtonElement | null;
      if (!btn) return;
      btn.click();
      btn.click();
      btn.click();
    });

    await expect(page.locator("#global-status")).toContainText("知识摘要已保存");
    expect(putCount).toBe(1);
  });
});

