"use strict";

// Run browser state regressions with Node built-ins; no browser, packages, or API are needed.
// 使用 Node 内置模块验证浏览器状态回归，不依赖浏览器、第三方包或真实 API。
const assert = require("node:assert/strict");
const { webcrypto } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/chat.js"), "utf8");

/** Return a minimal successful fetch response without opening a network connection.
 * 返回最小的成功 fetch 响应，全程不建立网络连接。
 */
function response(data) {
  return { ok: true, json: async () => data };
}

/** Create isolated page state and a controllable fetch transport for the real client script.
 * 为真实客户端脚本创建独立页面状态，以及可精确控制响应时序的模拟 fetch 传输。
 */
function createClient(handler) {
  const elements = new Map();

  /** Model only DOM operations used by the chat state machine, leaving layout to browsers.
   * 只模拟聊天状态机使用的 DOM 操作；不以此测试替代浏览器的视觉与布局验证。
   */
  class Element {
    /** Initialize mutable form, message, and accessibility state for one fake element.
     * 初始化单个模拟元素的表单、消息内容与无障碍属性状态。
     */
    constructor() {
      this.value = "";
      this.textContent = "";
      this.hidden = false;
      this.children = [];
      this.listeners = new Map();
      this.attributes = new Map();
      this.style = {};
      const classes = new Set();
      this.classList = {
        // Match forced class toggles used for the sidebar, spinner, and message layout.
        // 模拟侧栏、等待动画与消息布局所使用的显式类名切换。
        toggle(name, enabled) {
          if (enabled) classes.add(name);
          else classes.delete(name);
        },
        /** Add a CSS class, such as the offline state.
         * 添加 CSS 类名，例如离线状态。
         */
        add(name) { classes.add(name); },
        /** Read a class so production sidebar logic can inspect its state.
         * 读取类名是否存在，供真实侧栏逻辑判断当前状态。
         */
        contains(name) { return classes.has(name); },
      };
    }

    /** Capture event callbacks so tests exercise actual form and navigation handlers.
     * 保存事件回调，让测试直接执行真实表单与导航处理逻辑。
     */
    addEventListener(type, callback) { this.listeners.set(type, callback); }

    /** Track accessibility attributes written by the production client.
     * 记录真实客户端写入的无障碍属性。
     */
    setAttribute(name, value) { this.attributes.set(name, value); }

    /** Append message nodes in their rendered order.
     * 按渲染顺序追加消息节点。
     */
    append(...children) { this.children.push(...children); }

    /** Replace all rendered nodes, including clearing a previous history snapshot.
     * 替换全部渲染节点，也支持清空已有的历史快照。
     */
    replaceChildren(...children) { this.children = children; }

    /** Accept focus requests; keyboard and visual focus need browser-level verification.
     * 接收聚焦请求；键盘导航与视觉焦点仍应在真实浏览器中验证。
     */
    focus() {}
  }

  /** Reuse stable element identities across initialization and event callbacks.
   * 在脚本初始化和后续事件回调之间复用稳定的元素实例。
   */
  function element(id) {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  }

  /** Stub the initial health check and delegate business requests to the test scenario.
   * 固定初始化健康检查响应，将业务请求交给各测试场景控制。
   */
  async function fetchClient(url, options) {
    if (url === "/health") return response({ reply_mode: "fixed" });
    return handler(url, options);
  }

  // Execute the unchanged browser script in a fresh global scope for every test.
  // 每个测试都在新的全局作用域执行原始浏览器脚本，防止状态在测试之间泄漏。
  const context = vm.createContext({
    document: {
      getElementById: element,
      createElement: () => new Element(),
      querySelectorAll: () => [],
      querySelector: () => null,
      addEventListener() {},
      documentElement: { dataset: {} },
    },
    fetch: fetchClient,
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    localStorage: { getItem() {}, setItem() {} },
    AbortSignal, crypto: webcrypto, setTimeout, Date,
    btoa: value => Buffer.from(value, "binary").toString("base64"),
  });
  vm.runInContext(source, context);

  /** Evaluate setup actions and real navigation functions in the page's lexical scope.
   * 在页面自身的词法作用域执行测试准备操作和真实导航函数。
   */
  function run(code) { return vm.runInContext(code, context); }

  /** Submit through the registered handler, preserving the browser's preventDefault contract.
   * 通过实际注册的处理函数提交，并提供浏览器事件的 preventDefault 接口。
   */
  async function send(content = element("message").value) {
    element("message").value = content;
    await element("message-form").listeners.get("submit")({ preventDefault() {} });
  }

  return { element, run, send };
}

// Check the actual creation request, including a boundary that split an emoji before the fix.
// 检查真实创建会话请求，覆盖修复前会把 emoji 截成半个字符的边界。
for (const [name, content, expectedTitle] of [
  ["emoji boundary", "a" + "😀".repeat(35), "a" + "😀".repeat(35)],
  ["code point limit", "  " + "😀".repeat(80) + "  ", "😀".repeat(70)],
]) {
  test("new conversation title preserves Unicode: " + name, async () => {
    let title;
    const client = createClient(async (url, options) => {
      // Stop after capturing the title so this scenario needs no job or history fixtures.
      // 捕获标题后停止请求，避免为标题测试额外构造任务和历史记录。
      assert.equal(url, "/conversations");
      title = JSON.parse(options.body).title;
      throw new TypeError("Simulated response loss");
    });
    client.run("authorization = 'Basic test';");
    await client.send(content);
    assert.equal(title, expectedTitle);
    assert.equal(title.isWellFormed(), true);
  });
}

/** Simulate accepted submissions whose responses are lost, retaining every idempotency key.
 * 模拟服务端已接受提交但响应丢失的情况，并保存每次请求的去重标识。
 */
function uncertainSubmissions() {
  const submissions = [];
  const client = createClient(async (url, options) => {
    if (url.endsWith("/messages") && options.method === "POST") {
      submissions.push(JSON.parse(options.body));
      throw new TypeError("Simulated response loss after acceptance");
    }
    return response([]);
  });
  client.run("authorization = 'Basic test'; conversation = 'chat-a';");
  return { client, submissions };
}

test("reopening the current chat preserves the uncertain submission key", async () => {
  const { client, submissions } = uncertainSubmissions();
  await client.send("Process this once");
  await client.run("openConversation({ id: 'chat-a', title: 'Chat A' })");
  await client.send();
  assert.equal(submissions.length, 2);
  assert.equal(submissions[0].submission_key, submissions[1].submission_key);
});

test("returning from another chat restores its draft and uncertain submission together", async () => {
  const { client, submissions } = uncertainSubmissions();
  await client.send("Draft for A");
  await client.run("openConversation({ id: 'chat-b', title: 'Chat B' })");
  assert.equal(client.element("message").value, "");
  client.element("message").value = "Draft for B";
  await client.run("openConversation({ id: 'chat-a', title: 'Chat A' })");
  assert.equal(client.element("message").value, "Draft for A");
  await client.send();
  assert.equal(submissions[0].submission_key, submissions[1].submission_key);
  await client.run("openConversation({ id: 'chat-b', title: 'Chat B' })");
  assert.equal(client.element("message").value, "Draft for B");
});

test("signing out clears drafts and pending submissions before another account signs in", async () => {
  const { client } = uncertainSubmissions();
  await client.send("Private draft");
  client.run("newChat()");
  client.element("sign-out").listeners.get("click")();
  client.run("authorization = 'Basic another-account';");
  await client.run("openConversation({ id: 'chat-a', title: 'Chat A' })");
  assert.equal(client.element("message").value, "");
  assert.equal(client.run("pending"), null);
});

/** Exercise a delayed initial history read after a full send-and-reply workflow finishes.
 * 在完整发送和回复流程结束后再返回初始历史请求，验证旧响应不会覆盖新结果。
 */
async function delayedHistory({ reject = false, fullPage = false } = {}) {
  let resolveOld, rejectOld;
  const oldResponse = new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject; });
  let historyReads = 0;
  const user = { role: "user", content: "Newest message", seq: 1 };
  const assistant = { role: "assistant", content: "Saved reply", seq: 2 };
  const client = createClient(async (url, options) => {
    // Hold only the first history request; let later refreshes and the job finish normally.
    // 只暂停第一次历史请求，让后续刷新和后台任务状态正常完成。
    if (url.endsWith("/messages") && options.method === "POST")
      return response({ message_id: "message-1", job_id: "job-1" });
    if (url.includes("/messages?")) {
      historyReads++;
      if (historyReads === 1) return oldResponse;
      return response(historyReads === 2 ? [user] : [user, assistant]);
    }
    if (url === "/jobs/job-1") return response({ status: "SUCCEEDED" });
    if (url === "/conversations") return response([{ id: "chat-a", title: "Chat A" }]);
    throw new Error("Unexpected request: " + url);
  });
  client.run("authorization = 'Basic test';");
  const opening = client.run("openConversation({ id: 'chat-a', title: 'Chat A' })");
  await client.send(user.content);
  assert.equal(client.element("messages").children.length, 2);

  // A stale failure must also be ignored; a stale full page must not trigger more pages.
  // 旧请求失败也应被忽略；旧请求返回满页数据时，也不能继续发起后续分页读取。
  if (reject) rejectOld(new TypeError("Late history failure"));
  else resolveOld(response(fullPage ? Array.from({ length: 500 }, () => user) : []));
  await opening;
  assert.equal(client.element("messages").children.length, 2);
  assert.equal(client.element("messages").children[1].children[1].textContent, assistant.content);
  assert.equal(historyReads, 3);
}

test("old history cannot overwrite a newly saved reply", () => delayedHistory());
test("old history failures do not replace a newer successful state", () => delayedHistory({ reject: true }));
test("superseded history does not fetch additional stale pages", () => delayedHistory({ fullPage: true }));

test("saved proposal restores after a new page loads the same chat", async () => {
  const conversation = { id: "proposal-chat", title: "Ticket 391" };
  const proposal = {
    id: "saved-proposal", status: "WAITING_FOR_APPROVAL", ticket_id: "391", order_id: "1001",
    refund_amount_minor: "9223372036854775807", currency: "CAD", proposed_ticket_status: "RESOLVED",
    delivered_at: "2026-11-06T12:00:00Z", expires_at: "2026-11-12T12:00:00Z", policy_version: "DEMO-CAD-30D/v1",
    customer_reply: "<img src=x> 感谢您的耐心。",
  };
  // Both pages load the same saved response from the server.
  // 两个页面均从服务端读取相同的已保存历史。
  const rows = [{ seq: 1, role: "user", content: "Prepare a refund for ticket 391", proposal }];
  for (let page = 0; page < 2; page++) {
    const client = createClient(url => {
      assert.match(url, /^\/conversations\/proposal-chat\/messages\?/);
      return response(rows);
    });
    await client.run("authorization = 'Basic demo'; openConversation(" + JSON.stringify(conversation) + ")");
    const [, card] = client.element("messages").children;
    assert.equal(client.element("messages").children.length, 2);
    assert.equal(card.attributes.get("data-proposal-id"), proposal.id);
    assert.equal(card.children[1].textContent, "Waiting for approval");
    assert.equal(card.children[2].children[5].textContent, "CAD 92233720368547758.07");
    assert.equal(card.children[4].textContent, proposal.customer_reply);
    assert.equal(card.children[4].children.length, 0);
  }
});
