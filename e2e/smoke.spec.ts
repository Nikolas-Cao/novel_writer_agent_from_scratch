import { test, expect } from "@playwright/test";

test.describe("Novel Writer Agent UI smoke", () => {
  test("首页可打开且基础控件存在", async ({ page }) => {
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "Novel Writer Agent" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "项目列表" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "新建小说" })).toBeVisible();
    await expect(page.locator("section.panel-detail h2")).toHaveText("项目详情");

    await expect(page.locator("#btn-refresh-projects")).toBeVisible();
    await expect(page.locator("#btn-new-project")).toBeVisible();
    await expect(page.locator("#btn-generate-ideas")).toBeVisible();
    await expect(page.getByRole("button", { name: "生成概要" })).toBeVisible();
    await expect(page.locator("#btn-generate-outline")).toBeVisible();

    const illust = page.locator("#enable-chapter-illustrations-checkbox");
    await expect(illust).toBeVisible();
    await expect(illust).not.toBeChecked();

    await expect(page.locator("#project-list")).toBeVisible();
  });

  test("刷新项目列表不会报错（需要后端已启动）", async ({ page }) => {
    await page.goto("/");

    const status = page.locator("#global-status");
    await expect(status).toBeVisible();

    await page.locator("#btn-refresh-projects").click();

    // 成功时会包含“已更新”；失败时包含“失败/错误”
    await expect(status).not.toContainText("无法连接后端");
    await expect(status).not.toContainText("加载失败");
  });
});

