const { chromium } = require(process.argv[2]);
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const [browserPath, url, artifacts] = process.argv.slice(3);
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: browserPath});
  const context = await browser.newContext({viewport: {width: 1280, height: 900}});
  const page = await context.newPage();
  const errors = [], actions = [];
  page.on("pageerror", error => errors.push(error.message));
  async function waitText(selector, text) { await page.waitForFunction(({selector, text}) => document.querySelector(selector)?.textContent.includes(text), {selector, text}, {timeout: 8000}); }
  async function submit(text) { await page.locator("#message").fill(text); await page.locator("#send").click(); }
  async function finished() { await waitText("#task", "已完成"); await page.waitForFunction(() => !document.querySelector("#send").disabled); }
  try {
    await page.goto(url);
    await page.locator("#title").fill("P6 浏览器验收"); await page.locator("#new-session button").click();
    await page.waitForFunction(() => !!document.querySelector("#sessions").value);
    const first = await page.locator("#sessions").inputValue();
    await submit("hello browser"); await finished(); await waitText("#history", "Demo: hello browser"); actions.push("create session and complete normal reply");
    await submit("tool: 工具返回完整结果"); await finished(); await waitText("#history", "工具返回完整结果"); actions.push("tool result and persisted history");
    await submit("ask: <img src=x onerror=alert(1)>请选择颜色"); await page.getByRole("textbox", {name: "人工答复"}).waitFor();
    const before = await page.locator("#task").textContent();
    await page.reload(); await page.getByRole("textbox", {name: "人工答复"}).waitFor();
    assert.equal(await page.locator("#task").textContent(), before); assert.equal(await page.locator("#interactions img").count(), 0);
    actions.push("refresh resumes same task; untrusted content rendered as text");
    await page.locator("#title").fill("第二会话"); await page.locator("#new-session button").click();
    await page.waitForFunction(first => document.querySelector("#sessions").value !== first, first);
    await page.locator("#sessions").selectOption(first);
    await page.getByRole("textbox", {name: "人工答复"}).waitFor({timeout: 4000});
    await page.getByRole("textbox", {name: "人工答复"}).fill("蓝色"); await page.getByRole("button", {name: "回答", exact: true}).click();
    await finished(); await waitText("#history", "蓝色"); actions.push("switch sessions and answer original pending task");
    await submit("read: note.txt"); await page.getByRole("button", {name: "批准", exact: true}).click(); await finished(); await waitText("#history", "P6 actual workspace read"); actions.push("approve actual workspace tool");
    await submit("read: note.txt"); await page.getByRole("button", {name: "拒绝", exact: true}).click(); await finished(); await waitText("#history", "Approval declined"); actions.push("decline approval without tool effect");
    await submit("ask: cancel test"); await page.getByRole("textbox", {name: "人工答复"}).waitFor(); await page.locator("#cancel").click(); await waitText("#task", "已取消"); actions.push("cancel wakes human wait");
    await submit("fail: browser failure"); await waitText("#task", "失败"); await waitText("#task", "AUTH"); actions.push("display task failure");
    await submit("remember: 我喜欢蓝色"); await finished();
    await submit("memory: 蓝色"); await finished(); await waitText("#history", "Memory evidence");
    assert.equal(await page.getByText("Review complete", {exact: true}).count(), 0); assert.equal(await page.getByText("Retrieval complete", {exact: true}).count(), 0); actions.push("review and recall use shared runtime without background chat");
    await page.screenshot({path: path.join(artifacts, "p6-browser.png"), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.locator("#history").evaluate(element => { element.scrollTop = element.scrollHeight; });
    await page.screenshot({path: path.join(artifacts, "p6-browser-mobile.png"), fullPage: true});
    assert.deepEqual(errors, []); actions.push("desktop/mobile layout; no browser JS errors");
    fs.writeFileSync(path.join(artifacts, "p6-browser-results.json"), JSON.stringify({browser: await browser.version(), actions, errors}, null, 2));
    console.log(JSON.stringify({browser: await browser.version(), actions, errors}, null, 2));
  } catch (error) {
    await page.screenshot({path: path.join(artifacts, "p6-browser-failure.png"), fullPage: true});
    console.error(error); process.exitCode = 1;
  } finally { await context.close(); await browser.close(); }
})();
