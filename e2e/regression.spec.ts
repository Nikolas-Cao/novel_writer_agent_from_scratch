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

test.describe("UI regressions from past chats", () => {
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
    const nextChapterPattern = new RegExp(`/projects/${projectId}/chapters/next$`);
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

    const nextUrl = new RegExp(`/projects/${projectId}/chapters/next$`);
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

  test("打开项目后：章节列表中选中项应滚动到可见（最新章节）", async ({ page, request }) => {
    const picked = await findProjectWithMinChapters(request, 3);
    test.skip(!picked, "需要至少 3 章以验证滚动");
    const { projectId, chaptersCount } = picked!;
    await openProjectFromList(page, projectId);
    const selectedLi = page.locator("#chapter-list li.is-selected");
    await expect(selectedLi).toBeVisible();
    await expect(selectedLi).toHaveAttribute("data-index", String(chaptersCount - 1));
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
});

