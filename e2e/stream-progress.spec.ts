import { test, expect } from "@playwright/test";

test.describe("LLM 长耗时：流式进度请求", () => {
  test("生成大纲：POST 带 stream=1，且能消费 NDJSON 进度与结果", async ({ page }) => {
    const projectDetail = {
      project_id: "p-stream-outline",
      instruction: "stub",
      selected_plot_summary: "",
      outline_structure: { volumes: [] },
      chapters: [],
      current_chapter_index: 0,
      total_chapters: 12,
      chapter_word_target: 3000,
      enable_chapter_illustrations: false,
      created_at: 1700000000,
      token_usage: {},
    };

    let outlineStreamSeen = false;
    /** openProject 会再次 GET 项目；需与 NDJSON 结果一致，否则会把刚渲染的大纲冲掉 */
    let outlineReady = false;
    const outlineAfterGenerate = {
      volumes: [{ volume_title: "卷一", chapters: [{ title: "第1章", points: ["a"] }] }],
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
            projects: [{ project_id: "p-stream-outline", created_at: projectDetail.created_at }],
          }),
        });
        return;
      }
      if (method === "GET" && path === "/projects/p-stream-outline") {
        const body = outlineReady
          ? { ...projectDetail, outline_structure: outlineAfterGenerate, outline: "stub" }
          : projectDetail;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(body),
        });
        return;
      }
      if (method === "POST" && path === "/projects/p-stream-outline/outline") {
        expect(url.searchParams.get("stream")).toBe("1");
        outlineStreamSeen = true;
        outlineReady = true;
        const ndjson =
          '{"type":"progress","stage":"plan_outline","message":"stub-progress-msg"}\n' +
          '{"type":"result","body":{"outline_structure":{"volumes":[{"volume_title":"卷一","chapters":[{"title":"第1章","points":["a"]}]}]},"outline":"stub"}}\n';
        await route.fulfill({
          status: 200,
          contentType: "application/x-ndjson; charset=utf-8",
          body: ndjson,
        });
        return;
      }

      await route.continue();
    });

    await page.goto("/");
    await expect(page.locator("#instruction-input")).toBeVisible();

    await page
      .locator(`.project-open-btn[data-project-id="${projectDetail.project_id}"]`)
      .click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${projectDetail.project_id}`);

    await page.locator("#custom-summary-input").fill("自定义概要用于生成大纲");
    await page.locator("#btn-generate-outline").click();

    await expect(page.locator("#global-status")).toContainText("大纲生成完成", { timeout: 10_000 });
    await expect(page.locator("#outline-view")).toContainText("卷一");
    expect(outlineStreamSeen, "应请求带 stream=1 的大纲接口").toBe(true);
  });
});
