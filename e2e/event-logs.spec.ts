import { test, expect } from "@playwright/test";

test.describe("事件日志弹窗", () => {
  test("可打开弹窗并展开事件卡片查看完整内容（stub）", async ({ page }) => {
    const pid = "p-event-log-stub";
    const longContent =
      "根据某个很长的意图生成概要，包含了较多上下文说明用于验证卡片默认截断与点击后展开完整文本展示。";

    await page.route("**/projects**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const path = url.pathname.replace(/\/$/, "") || "/";
      const method = req.method();

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
            instruction: "stub",
            selected_plot_summary: "",
            outline_structure: {
              volumes: [{ volume_title: "第一卷", chapters: [{ title: "第一章", points: ["a"] }] }],
            },
            chapters: [{ index: 0, title: "第一章", word_count: 10 }],
            current_chapter_index: 0,
            total_chapters: 100,
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
          body: JSON.stringify({ index: 0, content: "# 第一章\n\n正文", meta: { index: 0, title: "第一章" } }),
        });
        return;
      }
      if (method === "GET" && path === `/projects/${pid}/events`) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            project_id: pid,
            events: [
              {
                event_id: "evt-1",
                ts: 1700000000,
                project_id: pid,
                chapter_index: null,
                event_name: "generate_plot_ideas",
                event_content: longContent,
                status: "success",
              },
            ],
          }),
        });
        return;
      }
      await route.continue();
    });

    await page.goto("/");
    await page.locator(`.project-open-btn[data-project-id="${pid}"]`).click();
    await expect(page.locator("#global-status")).toContainText(`已打开 ${pid}`);

    await expect(page.locator("#btn-view-event-logs")).toBeVisible();
    await page.locator("#btn-view-event-logs").click();
    await expect(page.locator("#event-log-modal")).toBeVisible();

    const card = page.locator(".event-log-card").first();
    await expect(card).toBeVisible();
    await expect(card).not.toHaveClass(/expanded/);
    await card.click();
    await expect(card).toHaveClass(/expanded/);
    await expect(card.locator(".event-log-card-content")).toContainText("根据某个很长的意图生成概要");
  });
});
