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


test("a refreshed page restores the HTTP-only session without storing credentials", async () => {
  const requests = [];
  const client = createClient(async (url, options) => {
    requests.push([url, options]);
    if (url === "/me") return response({ id: "alice-id", username: "alice" });
    if (url === "/conversations") return response([{ id: "saved", title: "Saved chat" }]);
    throw new Error(url);
  }, { sessionFetch: true, localStorage: { getItem() {}, setItem() { throw new Error("No credentials in storage"); } } });
  await until(() => visibleText(client.element("conversations")).includes("Saved chat"));
  assert.equal(client.element("account-name").textContent, "alice");
  assert.equal(client.run("authorization.id"), "alice-id");
  assert.equal(requests[0][1].credentials, "same-origin");
  assert.equal(requests[0][1].headers.Authorization, undefined);
  assert.equal(requests[1][1].headers["X-AgentHub-Account"], "alice-id");
});

test("a failed server logout keeps the account visible and a retry revokes it before clearing", async () => {
  let attempts = 0;
  const client = createClient(async url => {
    if (url === "/me") return { ok: false, status: 401 };
    if (url === "/auth/logout") return ++attempts === 1 ? { ok: false, status: 503 } : { ok: true, status: 204 };
    throw new Error(url);
  }, { sessionFetch: true });
  client.run("activateAccount({id:'alice-id',username:'alice'}); drafts.set('a',{content:'private'});");
  const logout = client.element("sign-out").listeners.get("click");
  await logout();
  assert.equal(client.run("accountId"), "alice-id");
  assert.match(client.element("notice").textContent, /Sign-out could not be completed/);
  await logout();
  assert.equal(client.run("accountId"), null);
  assert.equal(client.run("drafts.size"), 0);
});

test("a late session restoration cannot sign the user back in after logout", async () => {
  let finish;
  const client = createClient(async url => {
    if (url === "/me") return new Promise(resolve => { finish = resolve; });
    if (url === "/auth/logout") return { ok: true, status: 204 };
    throw new Error(url);
  }, { sessionFetch: true });
  client.run("activateAccount({id:'alice-id',username:'alice'})");
  await client.element("sign-out").listeners.get("click")();
  finish(response({ id: "alice-id", username: "alice" }));
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(client.run("authorization"), null);
  assert.equal(client.run("accountId"), null);
});

test("login sends the password once and subsequent requests use the cookie account context", async () => {
  const seen = [];
  const client = createClient(async (url, options) => {
    seen.push([url, options]);
    if (url === "/auth/login") return response({ id: "alice-id", username: "alice" });
    if (url === "/conversations") return response([]);
    throw new Error(url);
  });
  client.element("username").value = "alice";
  client.element("password").value = "private-test-password";
  await client.element("login-form").listeners.get("submit")({ preventDefault() {} });
  assert.equal(seen[0][0], "/auth/login");
  assert.equal(JSON.parse(seen[0][1].body).password, "private-test-password");
  assert.equal(seen[0][1].headers.Authorization, undefined);
  assert.equal(seen[0][1].headers["X-AgentHub-Request"], "1");
  assert.equal(seen[1][1].headers["X-AgentHub-Account"], "alice-id");
  assert.equal(client.element("password").value, "");
});

// Read visible strings from the minimal DOM without interpreting them as HTML.
// 从最小 DOM 读取可见文本，不将其解释为 HTML。
function visibleText(element) {
  return [element.textContent, ...element.children.map(visibleText)].join(" ");
}

test("right-click and ellipsis menus persist Unicode rename and pin changes", async () => {
  let saved = { id: "chat-a", title: "Original", is_pinned: false };
  const updates = [];
  const client = createClient(async (url, options) => {
    if (url === "/conversations/chat-a") {
      assert.equal(options.method, "PATCH");
      updates.push(JSON.parse(options.body));
      saved = { ...saved, ...updates.at(-1) };
      return response(saved);
    }
    if (url === "/conversations") return response([saved]);
    throw new Error("Unexpected request: " + url);
  });
  client.run("authorization = 'Basic test'; conversation = 'chat-a'; conversations = [{id:'chat-a',title:'Original',is_pinned:false}]; drawConversations()");
  let prevented = false;
  client.element("conversations").children[0].listeners.get("contextmenu")({preventDefault(){prevented=true;}, clientX:100, clientY:100});
  assert.equal(prevented, true);
  assert.equal(client.element("chat-menu").hidden, false);
  client.element("chat-rename").listeners.get("click")();
  assert.equal(client.element("chat-action-dialog").open, true);
  client.element("chat-name").value = "  团队 💬  ";
  await client.element("chat-action-form").listeners.get("submit")({preventDefault(){}});
  assert.equal(updates[0].title, "团队 💬");
  assert.equal(client.element("chat-title").textContent, "团队 💬");
  assert.equal(client.element("chat-action-dialog").open, false);
  client.element("conversations").children[0].children[1].listeners.get("click")();
  await client.element("chat-pin").listeners.get("click")();
  assert.equal(saved.is_pinned, true);
  assert.match(visibleText(client.element("conversations")), /📌/);
  client.element("conversations").children[0].children[1].listeners.get("click")();
  assert.equal(client.element("chat-pin").textContent, "Unpin");
  await client.element("chat-pin").listeners.get("click")();
  assert.equal(saved.is_pinned, false);
});

test("delete requires confirmation then clears the selected chat and its draft", async () => {
  let deletes = 0;
  const client = createClient(async (url, options) => {
    if (url === "/conversations/chat-a") {
      assert.equal(options.method, "DELETE");
      deletes++;
      return {ok:true,status:204};
    }
    if (url === "/conversations") return response([]);
    throw new Error("Unexpected request: " + url);
  });
  client.run("authorization = 'Basic test'; conversation = 'chat-a'; drafts.set('chat-a',{content:'Saved draft'}); conversations = [{id:'chat-a',title:'Delete me'}]; drawConversations()");
  const openDelete = () => {
    client.element("conversations").children[0].children[1].listeners.get("click")();
    client.element("chat-delete").listeners.get("click")();
  };
  openDelete();
  assert.equal(deletes, 0);
  client.element("chat-action-cancel").listeners.get("click")();
  assert.equal(deletes, 0);
  openDelete();
  await client.element("chat-action-form").listeners.get("submit")({preventDefault(){}});
  assert.equal(deletes, 1);
  assert.equal(client.run("conversation"), null);
  assert.equal(client.run("drafts.has('chat-a')"), false);
  assert.equal(client.element("chat-title").textContent, "New chat");
});

test("blocked delete preserves the conversation and explains pending work", async () => {
  const client = createClient(async () => ({ok:false,status:409}));
  client.run("authorization = 'Basic test'; conversation = 'chat-a'; conversations = [{id:'chat-a',title:'Pending'}]; drawConversations()");
  client.element("conversations").children[0].children[1].listeners.get("click")();
  client.element("chat-delete").listeners.get("click")();
  await client.element("chat-action-form").listeners.get("submit")({preventDefault(){}});
  assert.equal(client.run("conversation"), "chat-a");
  assert.equal(client.element("chat-action-dialog").open, true);
  assert.match(client.element("chat-action-error").textContent, /reject the pending proposal/);
  assert.equal(client.run("busy"), false);
});

test("older conversation list responses cannot overwrite a newer rename", async () => {
  let oldRead;
  const client = createClient(async () => new Promise(resolve => {oldRead = resolve;}));
  client.run("authorization = 'Basic test'");
  const reading = client.run("listConversations()");
  client.run("++conversationRead; conversations = [{id:'chat-a',title:'New name'}]; drawConversations()");
  oldRead(response([{id:"chat-a",title:"Old name"}]));
  await reading;
  assert.equal(client.run("conversations[0].title"), "New name");
});

test("saved activity renders separately and survives repeated history loads", () => {
  const client = createClient(async () => response([]));
  client.run(`showMessages([{
    role: "user", content: "Review my ticket",
    activity: { status: "SUCCEEDED", attempts: 1, steps: [
      { seq: 1, status: "completed", text: "Retrieved ticket and order" },
      { seq: 2, status: "completed", text: "<img src=x onerror=alert(1)>" }
    ] }
  }]);`);
  const panel = client.element("messages").children[1];
  assert.equal(panel.className, "execution-activity");
  assert.equal(panel.children[0].children.length, 2);
  assert.equal(panel.children[0].children[1].children[1].textContent, "<img src=x onerror=alert(1)>");
  assert.equal(panel.children[0].children[1].children[1].children.length, 0);
  client.run(`showMessages([{role:"user", content:"Review", activity:{status:"SUCCEEDED", attempts:1,
    steps:[{seq:1,status:"completed",text:"Retrieved ticket and order"}]}}]);`);
  assert.equal(client.element("messages").children.length, 2);
});

test("failed and queued activity never labels an unfinished step successful", () => {
  const client = createClient(async () => response([]));
  for (const status of ["FAILED", "QUEUED"]) {
    client.run(`showMessages([{role:"user", content:"Review", activity:{status:"${status}", attempts:2,
      steps:[{seq:1,status:"running",text:"Checking refund eligibility"}]}}]);`);
    const panel = client.element("messages").children[1];
    assert.equal(panel.children[0].children[0].className, status === "FAILED" ? "activity-stopped" : "activity-waiting");
    assert.match(visibleText(panel), /Processing attempt 2/);
    assert.doesNotMatch(visibleText(panel), /✓/);
  }
  const failed = client.run(`renderActivity({status:"FAILED",attempts:1,steps:[]}, {status:"INVALIDATED"})`);
  assert.match(visibleText(failed), /Proposal no longer valid/);
  assert.doesNotMatch(visibleText(failed), /Simulated refund applied/);
});

test("pushed activity refreshes saved progress once and restores it on reopening", async t => {
  let progressStream;
  let reads = 0;
  const client = createClient(async (url, options) => {
    if (url === "/conversations/chat-a/events?version=2") {
      progressStream = sseResponse(options.signal);
      return progressStream.response;
    }
    if (url.includes("/messages?")) {
      reads++;
      return response([{role:"user",content:"Review", activity:{status:"RUNNING",attempts:1,
        steps: reads > 1 ? [{seq:1,status:"completed",text:"Retrieved ticket and order"}] : []}}]);
    }
    if (url === "/conversations") return response([]);
    throw new Error("Unexpected request: " + url);
  }, streamingGlobals);
  t.after(() => client.run("newChat()"));
  await client.run("authorization = 'Basic test'; openConversation({id:'chat-a',title:'Chat A'})");
  await until(() => progressStream);
  progressStream.push('id: v2:3\nevent: agent.step_completed\ndata: {"conversation_id":"chat-a","type":"agent.step_completed","payload":{"job_id":"job-1"}}\n\n');
  progressStream.push('id: v2:3\nevent: agent.step_completed\ndata: {"conversation_id":"chat-a","type":"agent.step_completed","payload":{"job_id":"job-1"}}\n\n');
  await until(() => visibleText(client.element("messages")).includes("Retrieved ticket and order"));
  assert.equal(reads, 2);
  assert.equal(client.element("messages").children[1].children[0].children.length, 2);
  client.run("newChat()");
  assert.doesNotMatch(visibleText(client.element("messages")), /Retrieved ticket/);
  await client.run("openConversation({id:'chat-a',title:'Chat A'})");
  assert.match(visibleText(client.element("messages")), /Retrieved ticket and order/);
});

/** Return a minimal successful fetch response without opening a network connection.
 * 返回最小的成功 fetch 响应，全程不建立网络连接。
 */
function response(data) {
  return { ok: true, json: async () => data };
}

/** Create isolated page state and a controllable fetch transport for the real client script.
 * 为真实客户端脚本创建独立页面状态，以及可精确控制响应时序的模拟 fetch 传输。
 */
function createClient(handler, extraGlobals = {}) {
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
    showModal() { this.open = true; }
    close() { this.open = false; }
    getBoundingClientRect() { return { right: 100, bottom: 100 }; }
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
    if (url === "/me" && !extraGlobals.sessionFetch) return { ok: false, status: 401 };
    if (url === "/auth/logout" && !extraGlobals.sessionFetch) return { ok: true, status: 204 };
    if (url.endsWith("/messages?after_seq=0&limit=1") && !extraGlobals.snapshotFetch)
      return { ok: true, headers: { get: () => "v2:0" } };
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
    AbortSignal, crypto: webcrypto, setTimeout, clearTimeout, Date,
    window: { innerWidth: 1024, innerHeight: 768 },
    btoa: value => Buffer.from(value, "binary").toString("base64"),
    ...extraGlobals,
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
  await client.element("sign-out").listeners.get("click")();
  client.run("authorization = 'Basic another-account';");
  await client.run("openConversation({ id: 'chat-a', title: 'Chat A' })");
  assert.equal(client.element("message").value, "");
  assert.equal(client.run("pending"), null);
});

/** Exercise a delayed initial history read after a full send-and-reply workflow finishes.
 * 在完整发送和回复流程结束后再返回初始历史请求，验证旧响应不会覆盖新结果。
 */
async function delayedHistory({ reject = false, missing = false, fullPage = false } = {}) {
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
  else if (missing) resolveOld({ ok: false, status: 404 });
  else resolveOld(response(fullPage ? Array.from({ length: 500 }, () => user) : []));
  await opening;
  assert.equal(client.run("conversation"), "chat-a");
  assert.equal(client.element("messages").children.length, 2);
  assert.equal(client.element("messages").children[1].children[1].textContent, assistant.content);
  assert.equal(historyReads, 3);
}

test("old history cannot overwrite a newly saved reply", () => delayedHistory());
test("old history failures do not replace a newer successful state", () => delayedHistory({ reject: true }));
test("an obsolete history 404 does not clear the active chat", () => delayedHistory({ missing: true }));
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

for (const decision of ["APPROVE", "REJECT"]) {
  test("proposal card submits one " + decision + " and displays the durable result", async () => {
    const proposal = {
      id: "proposal", version: 1, status: "WAITING_FOR_APPROVAL", ticket_id: "391", order_id: "1001",
      refund_amount_minor: "12999", currency: "CAD", proposed_ticket_status: "RESOLVED",
      delivered_at: "2026-11-06T12:00:00Z", expires_at: "2026-11-12T12:00:00Z", policy_version: "DEMO-CAD-30D/v1",
      customer_reply: "The refund is eligible.",
    };
    let calls = 0;
    let jobReads = 0;
    let releaseDecision;
    const decisionResponse = new Promise(resolve => { releaseDecision = resolve; });
    const client = createClient(async (url, options) => {
      if (url === "/proposals/proposal/decision") {
        calls++;
        assert.deepEqual(JSON.parse(options.body), { decision, version: 1 });
        await decisionResponse;
        proposal.status = decision === "APPROVE" ? "QUEUED" : "REJECTED";
        if (decision === "APPROVE") proposal.resume_job_id = "resume-job";
        return response({});
      }
      if (url === "/jobs/resume-job") {
        // A brief outage must not strand the card before a later successful read.
        // 短暂故障后，后续成功读取仍应刷新方案卡片。
        if (++jobReads === 1) return { ok: false, status: 503 };
        proposal.status = "COMPLETED";
        return response({ status: "SUCCEEDED" });
      }
      assert.match(url, /^\/conversations\/refund-chat\/messages\?/);
      return response([{ seq: 1, role: "user", content: "Prepare a refund for ticket 391", proposal: { ...proposal } }]);
    });
    await client.run("authorization = 'Basic demo'; openConversation({id:'refund-chat', title:'Refund'})");
    const initialCard = client.element("messages").children[1];
    const buttons = initialCard.children[6].children;
    const button = buttons[decision === "APPROVE" ? 0 : 1];

    // Fire twice while the first request is in flight to exercise the client-side guard.
    // 首个请求尚未返回时再次点击，验证客户端的重复提交保护。
    const first = button.listeners.get("click")();
    await button.listeners.get("click")();
    assert.equal(calls, 1);
    assert.ok(buttons.every(button => button.disabled));
    releaseDecision();
    await first;

    const expected = decision === "APPROVE" ? "Completed" : "Rejected";
    for (let i = 0; i < 55 && client.element("messages").children[1].children[1].textContent !== expected; i++) {
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    const card = client.element("messages").children[1];
    assert.equal(card.children[1].textContent, expected);
    assert.ok(!card.children.some(child => child.className === "proposal-actions"));
  });
}

test("switching chats clears old proposal controls before history loads", async () => {
  const proposal = {
    id: "old-proposal", version: 1, status: "WAITING_FOR_APPROVAL", ticket_id: "391", order_id: "1001",
    refund_amount_minor: "12999", currency: "CAD", proposed_ticket_status: "RESOLVED",
    delivered_at: "2026-11-06T12:00:00Z", expires_at: "2026-11-12T12:00:00Z", policy_version: "DEMO-CAD-30D/v1",
    customer_reply: "The refund is eligible.",
  };
  let resolveHistory;
  let decisions = 0;
  const delayedHistory = new Promise(resolve => { resolveHistory = resolve; });
  const client = createClient(async url => {
    if (url.startsWith("/proposals/")) {
      decisions++;
      return response({});
    }
    if (url.startsWith("/conversations/old-chat/messages?")) {
      return response([{ seq: 1, role: "user", content: "Old request", proposal }]);
    }
    return delayedHistory;
  });
  await client.run("authorization = 'Basic demo'; openConversation({id:'old-chat', title:'Old'})");
  const staleButton = client.element("messages").children[1].children[6].children[0];
  const switching = client.run("openConversation({id:'new-chat', title:'New'})");
  assert.deepEqual(client.element("messages").children, []);

  // Even a queued click from a detached card cannot decide the old proposal.
  // 即使已移除卡片仍有排队的点击事件，也不得审批旧方案。
  await staleButton.listeners.get("click")();
  assert.equal(decisions, 0);
  resolveHistory(response([{ seq: 1, role: "user", content: "New request" }]));
  await switching;
  assert.equal(client.element("messages").children[0].children[1].textContent, "New request");
});

// Live-update tests provide the browser streaming APIs that the default harness omits.
// 实时更新测试额外提供默认测试环境未包含的浏览器流式接口。
const streamingGlobals = { AbortController, TextDecoderStream };

/** Build a controllable SSE response whose body errors when the request is aborted, like fetch.
 * 构造可控的 SSE 响应；与真实 fetch 一样，请求被中止时响应体会报错。
 */
function sseResponse(signal) {
  let controller;
  const body = new ReadableStream({ start(c) { controller = c; } });
  signal?.addEventListener("abort", () => {
    try { controller.error(new DOMException("Aborted", "AbortError")); } catch { /* already closed */ }
  });
  const encoder = new TextEncoder();
  return {
    response: { ok: true, status: 200, body },
    push: text => controller.enqueue(encoder.encode(text)),
    close: () => controller.close(),
  };
}

/** Wait until a condition holds, failing the test instead of hanging.
 * 等待条件成立；超时则让测试失败而不是挂起。
 */
async function until(check, ms = 3000) {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) throw new Error("Timed out waiting for condition");
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

test("a pushed job event completes a send without polling the job", async t => {
  let stream, jobPolls = 0, done = false;
  const user = { seq: 1, role: "user", content: "Hi" };
  const assistant = { seq: 2, role: "assistant", content: "Message received." };
  const client = createClient(async (url, options) => {
    if (url === "/conversations/chat-a/events?version=2") {
      stream = sseResponse(options.signal);
      return stream.response;
    }
    if (url.endsWith("/messages") && options.method === "POST") return response({ message_id: "m1", job_id: "job-1" });
    if (url.includes("/messages?")) return response(done ? [user, assistant] : [user]);
    if (url === "/conversations") return response([{ id: "chat-a", title: "Chat A" }]);
    if (url === "/jobs/job-1") { jobPolls++; return response({ status: "QUEUED" }); }
    throw new Error("Unexpected request: " + url);
  }, streamingGlobals);
  // Close the stream even when an assertion fails, so the runner cannot hang.
  // 即使断言失败也关闭事件流，避免测试进程挂起。
  t.after(() => client.run("newChat()"));
  await client.run("authorization = 'Basic test'; openConversation({ id: 'chat-a', title: 'Chat A' })");
  await until(() => stream);
  stream.push('retry: 2000\n\nevent: ready\ndata: {"cursor":"v2:0"}\n\n');
  await until(() => client.run("streamLive()"));
  assert.equal(client.element("connection-label").textContent, "Live updates");

  const sending = client.send("Hi");
  await new Promise(resolve => setTimeout(resolve, 50));
  done = true;
  // Split one frame across chunks to exercise buffering.
  // 将一个事件帧拆成两段发送，以验证缓冲解析。
  stream.push('id: v2:2\nevent: job.status_changed\ndata: {"conversation_id":"chat-a","type":"job.status_changed","payload":{"job_id":"job-1","status":"SUCC');
  stream.push('EEDED","last_error":null}}\n\n');
  await sending;
  assert.equal(jobPolls, 0);
  const rows = client.element("messages").children;
  assert.equal(rows[rows.length - 1].children[1].textContent, "Message received.");
  client.run("newChat()");
  assert.equal(client.element("connection-label").textContent, "Local workspace");
});

for (const detailsAvailable of [true, false]) {
  test(`a pushed job failure ${detailsAvailable ? "shows the saved reason" : "falls back if the job read fails"}`, async t => {
    let stream, jobReads = 0;
    const reason = "Worker claim limit reached after interruption";
    const client = createClient(async (url, options) => {
      if (url === "/conversations/chat-a/events?version=2") {
        stream = sseResponse(options.signal);
        return stream.response;
      }
      if (url.endsWith("/messages") && options.method === "POST") return response({ job_id: "job-1" });
      if (url.includes("/messages?")) return response([{ seq: 1, role: "user", content: "Hi" }]);
      if (url === "/conversations") return response([]);
      if (url === "/jobs/job-1") {
        jobReads++;
        return detailsAvailable
          ? response({ id: "job-1", status: "FAILED", attempts: 3, last_error: reason })
          : { ok: false, status: 503 };
      }
      throw new Error("Unexpected request: " + url);
    }, streamingGlobals);
    t.after(() => client.run("stopStream()"));
    await client.run("authorization = 'Basic test'; openConversation({id:'chat-a', title:'A'})");
    await until(() => stream);
    stream.push('event: ready\ndata: {"cursor":"v2:0"}\n\n');
    await until(() => client.run("streamLive()"));

    const sending = client.send("Hi");
    await until(() => client.run("jobWaiters.has('job-1')"));
    stream.push('id: v2:1\nevent: job.status_changed\ndata: ' + JSON.stringify({
      seq: "1", type: "job.status_changed", conversation_id: "chat-a",
      payload: { job_id: "job-1", source_message_id: "m1", kind: "PROCESS_MESSAGE", status: "FAILED", attempts: 3 },
    }) + '\n\n');
    await sending;

    assert.equal(client.element("notice").textContent, detailsAvailable ? reason : "The reply could not be completed.");
    assert.equal(jobReads, 1);
    assert.equal(client.run("busy"), false);
  });
}

test("a dropped stream reconnects with the last complete durable event ID", async t => {
  const attempts = [];
  const client = createClient(async (url, options) => {
    if (url === "/conversations/chat-a/events?version=2") {
      const stream = sseResponse(options.signal);
      attempts.push({ headers: options.headers, stream });
      return stream.response;
    }
    if (url.includes("/messages?")) return response([]);
    if (url === "/conversations") return response([]);
    throw new Error("Unexpected request: " + url);
  }, streamingGlobals);
  // Close the stream even when an assertion fails, so the runner cannot hang.
  // 即使断言失败也关闭事件流，避免测试进程挂起。
  t.after(() => client.run("newChat()"));
  await client.run("authorization = 'Basic test'; openConversation({ id: 'chat-a', title: 'Chat A' })");
  await until(() => attempts.length === 1);
  assert.equal(attempts[0].headers["Last-Event-ID"], "v2:0");
  attempts[0].stream.push('event: ready\ndata: {"cursor":"v2:2"}\n\n: keepalive\n\n');
  attempts[0].stream.push('id: v2:3\nevent: message.created\ndata: {"conversation_id":"chat-a","type":"message.created","payload":{"message_seq":3}}\n\n');
  await until(() => client.run("stream.cursor") === "v2:3");
  attempts[0].stream.close();
  await until(() => client.element("connection-label").textContent === "Reconnecting…");
  await until(() => attempts.length === 2);
  assert.equal(attempts[1].headers["Last-Event-ID"], "v2:3");
  client.run("newChat()");
});

test("a history 404 clears a removed chat and refreshes the sidebar without SSE", async () => {
  let sidebarReads = 0;
  const client = createClient(async url => {
    if (url.startsWith("/conversations/chat-a/messages?")) return { ok: false, status: 404 };
    if (url === "/conversations") {
      sidebarReads++;
      return response([{ id: "chat-b", title: "Other chat" }]);
    }
    throw new Error("Unexpected request: " + url);
  });
  client.run(`authorization = 'Basic test';
    conversations = [{ id: 'chat-a', title: 'Removed chat' }];
    drafts.set('chat-a', { content: 'Old draft', pending: { submission_key: 'old-key' } });
    drafts.set('chat-b', { content: 'Other draft' });
    drawConversations();`);
  await client.element("conversations").children[0].children[0].listeners.get("click")();
  assert.equal(client.run("conversation"), null);
  assert.equal(client.run("pending"), null);
  assert.equal(client.run("drafts.has('chat-a')"), false);
  assert.equal(client.run("drafts.has('chat-b')"), true);
  assert.equal(client.element("message").value, "");
  assert.equal(client.element("message").disabled, false);
  assert.equal(client.element("chat-title").textContent, "New chat");
  assert.equal(client.element("messages").children.length, 0);
  assert.match(client.element("notice").textContent, /chat was removed/);
  assert.equal(sidebarReads, 1);
  await until(() => visibleText(client.element("conversations")).includes("Other chat"));
  assert.doesNotMatch(visibleText(client.element("conversations")), /Removed chat/);
});

// Same history 404 while the SSE v2 stream is starting: the chat is retired and its stream closed for good.
// 同样的历史 404，但 SSE v2 事件流正在建立：会话被清理，事件流被关闭且不再重连。
test("a history 404 with the v2 stream starting clears the chat and closes its stream", async t => {
  let sidebarReads = 0;
  const opened = [];
  const client = createClient(async (url, options) => {
    if (url === "/conversations/chat-a/messages?after_seq=0&limit=500") return { ok: false, status: 404 };
    if (url === "/conversations/chat-a/events?version=2") { opened.push(options.signal); return sseResponse(options.signal).response; }
    if (url === "/conversations") {
      sidebarReads++;
      return response([{ id: "chat-b", title: "Other chat" }]);
    }
    throw new Error("Unexpected request: " + url);
  }, streamingGlobals);
  t.after(() => client.run("newChat()"));
  client.run(`authorization = 'Basic test';
    conversations = [{ id: 'chat-a', title: 'Removed chat' }];
    drafts.set('chat-a', { content: 'Old draft', pending: { submission_key: 'old-key' } });
    drafts.set('chat-b', { content: 'Other draft' });
    drawConversations();`);
  await client.element("conversations").children[0].children[0].listeners.get("click")();
  assert.equal(client.run("conversation"), null);
  assert.equal(client.run("pending"), null);
  assert.equal(client.run("drafts.has('chat-a')"), false);
  assert.equal(client.run("drafts.has('chat-b')"), true);
  assert.equal(client.element("message").disabled, false);
  assert.equal(client.element("chat-title").textContent, "New chat");
  assert.equal(client.element("messages").children.length, 0);
  assert.match(client.element("notice").textContent, /chat was removed/);
  assert.equal(client.run("stream"), null);
  await until(() => visibleText(client.element("conversations")).includes("Other chat"));
  assert.doesNotMatch(visibleText(client.element("conversations")), /Removed chat/);
  // Any stream request that started was aborted, and none reopens after the reconnect delay.
  // 已发出的事件流请求都被中止，等过重连间隔后也不会重新打开。
  await new Promise(resolve => setTimeout(resolve, 400));
  assert.ok(opened.length <= 1);
  assert.ok(opened.every(signal => signal.aborted));
  assert.equal(sidebarReads, 1);
});

test("a missing conversation stream clears the chat without retrying", async t => {
  let opens = 0;
  const client = createClient(async url => {
    if (url === "/conversations/chat-a/events?version=2") { opens++; return { ok: false, status: 404 }; }
    if (url.includes("/messages?")) return response([]);
    if (url === "/conversations") return response([]);
    throw new Error("Unexpected request: " + url);
  }, streamingGlobals);
  // Close the stream even when an assertion fails, so the runner cannot hang.
  // 即使断言失败也关闭事件流，避免测试进程挂起。
  t.after(() => client.run("newChat()"));
  await client.run("authorization = 'Basic test'; openConversation({ id: 'chat-a', title: 'Chat A' })");
  await new Promise(resolve => setTimeout(resolve, 400));
  assert.equal(opens, 1);
  assert.equal(client.run("streamLive()"), false);
  assert.equal(client.run("conversation"), null);
  assert.equal(client.element("messages").children.length, 0);
  client.run("newChat()");
});

// Both deletion signals must release the old submission without touching a newer draft.
// 两种删除信号都必须释放旧提交，且不覆盖后来输入的草稿。
for (const delivery of ["event", "reconnect-404"]) {
  for (const lateFailure of [false, true]) {
    test(`remote deletion via ${delivery} releases an in-flight send (late failure: ${lateFailure})`, async t => {
      let activeStream, finishSubmit, failSubmit, opens = 0, sidebarReads = 0;
      const client = createClient(async (url, options) => {
        if (url === "/conversations/chat-a/events?version=2") {
          if (++opens > 1 && delivery === "reconnect-404") return {ok:false,status:404};
          activeStream = sseResponse(options.signal);
          return activeStream.response;
        }
        if (url.endsWith("/messages") && options.method === "POST") {
          return new Promise((resolve, reject) => { finishSubmit = resolve; failSubmit = reject; });
        }
        if (url.includes("/messages?")) return response([{role:"user",content:"Old history",seq:1}]);
        if (url === "/conversations") { sidebarReads++; return response([{id:"chat-b",title:"Other chat"}]); }
        throw new Error("Unexpected request: " + url);
      }, streamingGlobals);
      t.after(() => { finishSubmit?.(response({job_id:"old-job"})); client.run("newChat()"); });
      await client.run("authorization = 'Basic test'; conversations = [{id:'chat-a',title:'Old chat'}]; openConversation(conversations[0])");
      await until(() => activeStream);
      activeStream.push('event: ready\ndata: {"cursor":"v2:1"}\n\n');
      await until(() => client.run("streamLive()"));
      const sending = client.send("In-flight draft");
      await until(() => finishSubmit);
      client.run("drafts.set('chat-a', {content:'Old draft'}); drafts.set('chat-b', {content:'Other draft'})");
      assert.equal(client.run("busy"), true);
      if (delivery === "event") activeStream.push('event: conversation.deleted\ndata: {"conversation_id":"chat-a"}\n\n');
      else activeStream.close();
      await until(() => client.run("conversation") === null);
      await until(() => sidebarReads > 0);
      assert.equal(client.run("busy"), false);
      assert.equal(client.run("awaitingReply"), false);
      assert.equal(client.run("pending"), null);
      assert.equal(client.run("drafts.has('chat-a')"), false);
      assert.equal(client.run("drafts.has('chat-b')"), true);
      assert.equal(client.element("message").disabled, false);
      assert.equal(client.element("new-chat").disabled, false);
      assert.equal(client.element("messages").children.length, 0);
      assert.equal(client.element("chat-title").textContent, "New chat");
      assert.doesNotMatch(visibleText(client.element("conversations")), /Old chat/);
      client.element("message").value = "New draft";
      client.run("resizeComposer()");
      assert.equal(client.element("send").disabled, false);
      if (lateFailure) failSubmit(new TypeError("Old request failed"));
      else finishSubmit(response({job_id:"old-job", message_id:"old-message"}));
      await sending;
      assert.equal(client.run("busy"), false);
      assert.equal(client.element("message").value, "New draft");
      assert.equal(client.element("job-status").textContent, "");
      assert.doesNotMatch(client.element("notice").textContent, /Old request failed/);
    });
  }
}

// Reviewer timing: history renders, then the cursor snapshot finds the chat removed in another tab.
// Review 中的时序：历史已显示，随后获取游标的快照发现会话已在其他标签页被删除。
test("a snapshot 404 after history renders clears the removed chat without opening its stream", async t => {
  let resolveSnapshot, opens = 0, sidebarReads = 0;
  const client = createClient(async (url, options) => {
    if (url === "/conversations/chat-a/messages?after_seq=0&limit=1") {
      return new Promise(resolve => { resolveSnapshot = resolve; });
    }
    if (url === "/conversations/chat-a/messages?after_seq=0&limit=500") {
      return response([{ role: "user", content: "Old history", seq: 1 }]);
    }
    if (url.startsWith("/conversations/chat-a/events")) { opens++; return sseResponse(options.signal).response; }
    if (url === "/conversations") { sidebarReads++; return response([{ id: "chat-b", title: "Other chat" }]); }
    throw new Error("Unexpected request: " + url);
  }, { ...streamingGlobals, snapshotFetch: true });
  t.after(() => { resolveSnapshot?.({ ok: false, status: 404 }); client.run("newChat()"); });
  await client.run("authorization = 'Basic test'; conversations = [{id:'chat-a',title:'Old chat'}, {id:'chat-b',title:'Other chat'}]; drawConversations()");
  client.run("drafts.set('chat-a', {content:'Old draft'}); drafts.set('chat-b', {content:'Other draft'})");
  await client.run("openConversation(conversations[0])");

  // The ordinary history read finished first, so chat-a's old message is on screen.
  // 普通历史请求先完成，chat-a 的旧消息已经显示。
  assert.equal(client.run("conversation"), "chat-a");
  assert.match(visibleText(client.element("messages")), /Old history/);
  assert.match(visibleText(client.element("conversations")), /Old chat/);
  await until(() => resolveSnapshot);
  assert.equal(sidebarReads, 0);

  resolveSnapshot({ ok: false, status: 404 });
  await until(() => client.run("conversation") === null);
  await until(() => sidebarReads > 0);
  assert.equal(client.element("messages").children.length, 0);
  assert.doesNotMatch(visibleText(client.element("messages")), /Old history/);
  assert.equal(client.element("chat-title").textContent, "New chat");
  assert.equal(client.run("drafts.has('chat-a')"), false);
  assert.equal(client.run("drafts.has('chat-b')"), true);
  assert.doesNotMatch(visibleText(client.element("conversations")), /Old chat/);
  assert.match(visibleText(client.element("conversations")), /Other chat/);
  assert.equal(client.element("notice").textContent, "This chat was removed or is no longer available.");
  assert.equal(client.run("stream"), null);
  // No stream was ever opened for the removed chat, even after the reconnect delay.
  // 即使等过重连间隔，也从未为已删除的会话打开事件流。
  await new Promise(resolve => setTimeout(resolve, 400));
  assert.equal(opens, 0);
  assert.equal(client.element("message").disabled, false);
  client.element("message").value = "New draft";
  client.run("resizeComposer()");
  assert.equal(client.element("send").disabled, false);
});

// A late 404 for a chat the user already left must not clear the chat opened since.
// 用户已离开的会话迟到返回 404 时，不能清除之后打开的会话。
for (const stage of ["history", "snapshot", "stream"]) {
  test(`a late ${stage} 404 from a previous chat leaves the newly opened chat intact`, async t => {
    let resolveLate, sidebarReads = 0;
    const held = () => new Promise(resolve => { resolveLate = resolve; });
    const client = createClient(async (url, options) => {
      if (url === "/conversations/chat-a/messages?after_seq=0&limit=1") {
        return stage === "snapshot" ? held() : { ok: true, headers: { get: () => "v2:0" } };
      }
      if (url === "/conversations/chat-a/events?version=2") {
        return stage === "stream" ? held() : sseResponse(options.signal).response;
      }
      if (url === "/conversations/chat-b/messages?after_seq=0&limit=1") return { ok: true, headers: { get: () => "v2:0" } };
      if (url === "/conversations/chat-b/events?version=2") return sseResponse(options.signal).response;
      if (url === "/conversations/chat-a/messages?after_seq=0&limit=500") {
        return stage === "history" ? held() : response([{ role: "user", content: "Chat A text", seq: 1 }]);
      }
      if (url === "/conversations/chat-b/messages?after_seq=0&limit=500") return response([{ role: "user", content: "Chat B text", seq: 1 }]);
      if (url === "/conversations") { sidebarReads++; return response([]); }
      throw new Error("Unexpected request: " + url);
    }, { ...streamingGlobals, snapshotFetch: true });
    t.after(() => { resolveLate?.({ ok: false, status: 404 }); client.run("newChat()"); });
    await client.run("authorization = 'Basic test'; conversations = [{id:'chat-a',title:'Chat A'}, {id:'chat-b',title:'Chat B'}]");
    // A held history read keeps openConversation pending, so only await it for the other stages.
    // 历史请求被挂起时 openConversation 不会结束，因此只在其他阶段等待它完成。
    const opening = client.run("openConversation(conversations[0])");
    if (stage !== "history") await opening;
    await until(() => resolveLate);
    client.element("message").value = "Draft for B";
    await client.run("openConversation(conversations[1])");
    client.element("message").value = "Draft for B";
    assert.equal(client.run("conversation"), "chat-b");

    resolveLate({ ok: false, status: 404 });
    if (stage === "history") await opening;
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(client.run("conversation"), "chat-b");
    assert.equal(client.run("stream.conversation"), "chat-b");
    assert.match(visibleText(client.element("messages")), /Chat B text/);
    assert.equal(client.element("chat-title").textContent, "Chat B");
    assert.equal(client.element("message").value, "Draft for B");
    assert.match(visibleText(client.element("conversations")), /Chat B/);
    assert.notEqual(client.element("notice").textContent, "This chat was removed or is no longer available.");
    assert.equal(sidebarReads, 0);
  });
}

test("a failed refresh after a pushed message retries while the stream only sends heartbeats", async t => {
  let stream, historyReads = 0;
  const client = createClient(async (url, options) => {
    if (url === "/conversations/chat-a/events?version=2") {
      stream = sseResponse(options.signal);
      return stream.response;
    }
    if (url.includes("/messages?")) {
      historyReads++;
      // The initial open and the ready refresh succeed; the message-triggered read fails once.
      // 首次打开与 ready 刷新成功；由消息事件触发的读取失败一次。
      if (historyReads === 3) return { ok: false, status: 503 };
      return response(historyReads < 3 ? [] : [{ seq: 1, role: "user", content: "From another tab" }]);
    }
    if (url === "/conversations") return response([]);
    throw new Error("Unexpected request: " + url);
  }, streamingGlobals);
  // Close the stream even when an assertion fails, so the runner cannot hang.
  // 即使断言失败也关闭事件流，避免测试进程挂起。
  t.after(() => client.run("newChat()"));
  await client.run("authorization = 'Basic test'; openConversation({ id: 'chat-a', title: 'Chat A' })");
  await until(() => stream);
  stream.push('event: ready\ndata: {"cursor":"v2:0"}\n\n');
  await until(() => historyReads === 2);
  stream.push('id: v2:1\nevent: message.created\ndata: {"conversation_id":"chat-a","type":"message.created","payload":{"message_seq":1}}\n\n: keepalive\n\n');
  await until(() => historyReads === 3);
  await until(() => client.element("messages").children.length === 1, 4000);
  assert.equal(client.element("messages").children[0].children[1].textContent, "From another tab");
  client.run("newChat()");
});

test("a polled job state is not replaced by an older cached state", async () => {
  let polls = 0;
  const client = createClient(async url => {
    if (url === "/jobs/job-1") { polls++; return response({ status: "RUNNING", attempts: 1 }); }
    throw new Error("Unexpected request: " + url);
  });
  client.run("noteJob({ id: 'job-1', status: 'QUEUED', attempts: 0 })");
  assert.equal((await client.run("nextJob('job-1', 'QUEUED')")).status, "RUNNING");
  assert.equal((await client.run("nextJob('job-1', 'RUNNING')")).status, "RUNNING");
  assert.equal(polls, 2);
  // A stale push is ignored, while a retry (QUEUED after one claim) still moves forward.
  // 旧的推送会被忽略；而重试（领取一次后回到 QUEUED）仍视为向前推进。
  client.run("noteJob({ id: 'job-1', status: 'QUEUED', attempts: 0 })");
  assert.equal(client.run("jobStates.get('job-1').status"), "RUNNING");
  client.run("noteJob({ id: 'job-1', status: 'QUEUED', attempts: 1 })");
  assert.equal(client.run("jobStates.get('job-1').status"), "QUEUED");
});

test("a 401 history refresh stops retrying and asks the user to sign in again", async t => {
  let stream, historyReads = 0, loginOpened = 0;
  const client = createClient(async (url, options) => {
    if (url === "/conversations/chat-a/events?version=2") {
      stream = sseResponse(options.signal);
      return stream.response;
    }
    if (url.includes("/messages?")) {
      historyReads++;
      return historyReads < 3 ? response([]) : { ok: false, status: 401 };
    }
    if (url === "/conversations") return response([]);
    throw new Error("Unexpected request: " + url);
  }, streamingGlobals);
  t.after(() => client.run("newChat()"));
  client.element("login-panel").showModal = () => { loginOpened++; };
  await client.run("authorization = 'Basic test'; openConversation({ id: 'chat-a', title: 'Chat A' })");
  await until(() => stream);
  stream.push('event: ready\ndata: {"cursor":"v2:0"}\n\n');
  await until(() => historyReads === 2);
  stream.push('id: v2:1\nevent: message.created\ndata: {"conversation_id":"chat-a","type":"message.created","payload":{"message_seq":1}}\n\n');
  await until(() => loginOpened === 1);
  assert.equal(client.run("stream"), null);
  assert.equal(client.element("notice").textContent, "Your session has expired. Please sign in again.");
  // The first backoff retry would have fired after one second.
  // 若仍在退避重试，第一次重试会在一秒后发出。
  await new Promise(resolve => setTimeout(resolve, 1500));
  assert.equal(historyReads, 3);
});

test("a 401 stream asks for sign-in and resumes live updates after signing in", async t => {
  let opens = 0, loginOpened = 0;
  const client = createClient(async (url, options) => {
    if (url === "/conversations/chat-a/events?version=2") {
      opens++;
      if (opens === 1) return { ok: false, status: 401 };
      return sseResponse(options.signal).response;
    }
    if (url === "/auth/login") return response({ id: "alice-id", username: "alice" });
    if (url.includes("/messages?") || url === "/conversations") return response([]);
    throw new Error("Unexpected request: " + url);
  }, streamingGlobals);
  t.after(() => client.run("stopStream()"));
  client.element("login-panel").showModal = () => { loginOpened++; };
  client.element("login-panel").close = () => {};
  const login = client.element("login-form").listeners.get("submit");
  client.element("username").value = "alice";
  client.element("password").value = "old-password";
  await login({ preventDefault() {} });
  await client.run("openConversation({ id: 'chat-a', title: 'Chat A' })");
  client.element("message").value = "Alice draft";
  await until(() => loginOpened === 1);
  await new Promise(resolve => setTimeout(resolve, 300));
  assert.equal(opens, 1);

  client.element("password").value = "new-password";
  await login({ preventDefault() {} });
  await until(() => opens === 2);
  assert.equal(client.element("notice").textContent, "");
  assert.equal(client.element("chat-title").textContent, "Chat A");
  assert.equal(client.element("message").value, "Alice draft");
});

test("signing in as another account after a 401 clears the old workspace during a send", async t => {
  let stream, loginOpened = 0, streamOpens = 0, historyReads = 0, aliceJobPolls = 0;
  let aliceSubmission, signedInAccount = "alice";
  const client = createClient(async (url, options) => {
    if (url === "/auth/login") signedInAccount = JSON.parse(options.body).username;
    const isBob = signedInAccount === "bob";
    if (url === "/auth/login") return response(isBob ? { id: "bob-id", username: "bob" } : { id: "alice-id", username: "alice" });
    if (url === "/conversations") return response(isBob ? [{ id: "chat-b", title: "Bob chat" }] : [{ id: "chat-a", title: "Alice chat" }]);
    if (url === "/conversations/chat-a/events?version=2") {
      streamOpens++;
      stream = sseResponse(options.signal);
      return stream.response;
    }
    if (url === "/conversations/chat-a/messages" && options.method === "POST") {
      return new Promise(resolve => { aliceSubmission = resolve; });
    }
    if (url.startsWith("/conversations/chat-a/messages?")) {
      historyReads++;
      if (historyReads > 1) return { ok: false, status: 401 };
      return response([{ seq: 1, role: "user", content: "Alice history" }]);
    }
    if (url === "/jobs/alice-job") {
      aliceJobPolls++;
      return response({ id: "alice-job", status: "SUCCEEDED" });
    }
    throw new Error("Unexpected request: " + url);
  }, streamingGlobals);
  t.after(() => {
    client.run("stopStream()");
    aliceSubmission?.(response({ job_id: "alice-job" }));
  });
  client.element("login-panel").showModal = () => { loginOpened++; };
  client.element("login-panel").close = () => {};
  const login = client.element("login-form").listeners.get("submit");
  client.element("username").value = "alice";
  client.element("password").value = "old-password";
  await login({ preventDefault() {} });
  await client.run("openConversation({ id: 'chat-a', title: 'Alice chat' })");
  assert.equal(client.element("messages").children[0].children[1].textContent, "Alice history");
  const sending = client.send("Alice draft");
  await until(() => aliceSubmission);

  stream.push('event: ready\ndata: {"cursor":"v2:1"}\n\n');
  await until(() => loginOpened === 1);
  client.element("username").value = "bob";
  client.element("password").value = "password";
  await login({ preventDefault() {} });
  assert.equal(client.element("account-name").textContent, "bob");
  assert.equal(client.element("conversations").children[0].children[0].textContent, "Bob chat");
  assert.equal(client.element("chat-title").textContent, "New chat");
  assert.equal(client.element("messages").children.length, 0);
  assert.equal(client.element("message").value, "");
  assert.equal(client.run("pending"), null);
  assert.equal(client.run("busy"), false);
  assert.equal(streamOpens, 1);

  client.element("message").value = "Bob draft";
  aliceSubmission(response({ job_id: "alice-job" }));
  await sending;
  assert.equal(client.element("message").value, "Bob draft");
  assert.equal(client.element("notice").textContent, "");
  assert.equal(aliceJobPolls, 0);
});

test("a rejected snapshot cursor is replaced and replayed duplicates are ignored", async t => {
  const cursors = [];
  let snapshots = 0;
  const client = createClient(async (url, options) => {
    if (url.endsWith("&limit=1")) {
      snapshots++;
      const value = snapshots === 1 ? "v2:99" : "v2:9007199254740992";
      return { ok: true, headers: { get: key => key === "X-Conversation-Event-Cursor" ? value : null } };
    }
    if (url.endsWith("/events?version=2")) {
      cursors.push(options.headers["Last-Event-ID"]);
      if (cursors.length === 1) return { status: 422, ok: false };
      return sseResponse(options.signal).response;
    }
    return response([]);
  }, { ...streamingGlobals, snapshotFetch: true });
  t.after(() => client.run("stopStream()"));
  await client.run("authorization = 'Basic test'; openConversation({id:'chat-a', title:'A'})");
  await until(() => cursors.length === 2);
  assert.deepEqual(cursors, ["v2:99", "v2:9007199254740992"]);
  const changes = client.run(`(() => {
    let changes = 0;
    scheduleRefresh = () => changes++;
    const event = { id: "v2:9007199254740993", event: "agent.step_completed",
      data: JSON.stringify({ conversation_id: "chat-a", type: "agent.step_completed", payload: {} }) };
    handleFrame(stream, event);
    handleFrame(stream, event);
    return changes;
  })()`);
  assert.equal(changes, 1);
  assert.equal(client.run("stream.cursor"), "v2:9007199254740993");
});

/** Open the refund details dialog from a history row and read its fields as label/value pairs.
 * 从历史消息打开退款详情弹窗，并以“标签/值”形式读取其中的字段。
 */
function executionDetailClient(detail) {
  let reads = 0;
  const client = createClient(async url => {
    assert.equal(url, "/executions/ex-1");
    reads++;
    return response(typeof detail === "function" ? detail() : detail);
  });
  const dialog = client.element("execution-panel");
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; };
  client.run("showMessages([{role:'user', content:'Refund', execution_id:'ex-1'}])");
  const button = client.element("messages").children[0].children[2];
  async function open() {
    await button.listeners.get("click")();
    const children = client.element("execution-fields").children;
    const fields = new Map();
    for (let i = 0; i < children.length; i += 2) fields.set(children[i].textContent, children[i + 1].textContent);
    return fields;
  }
  return { client, dialog, open, reads: () => reads };
}

const ORDER_CHANGED_TEXT = "The order changed after the proposal was prepared, so it was not applied.";
const GENERIC_INVALIDATION_TEXT = "The proposal is no longer valid, so it was not applied.";

test("an approved proposal invalidated by an order change shows why and no applied refund", async () => {
  const { dialog, open, reads } = executionDetailClient({
    order_id: "1001", outcome: "ELIGIBLE", reason_codes: [], refund_amount_minor: "12999", currency: "CAD",
    status: "INVALIDATED", invalidation_reason: "ORDER_CHANGED", revalidated_at: "2026-11-21T15:30:00Z",
    decision: { decision: "APPROVE" }, resume_job_status: "SUCCEEDED", result: null,
  });
  const fields = await open();
  assert.equal(fields.get("Workflow status"), "INVALIDATED");
  assert.equal(fields.get("Proposal invalidated"), ORDER_CHANGED_TEXT);
  assert.equal(fields.get("Verified at"), new Date("2026-11-21T15:30:00Z").toLocaleString());
  // Approval and a successful continuation are shown, but they do not prove a refund.
  // 显示审批与后续任务成功，但二者都不能证明已经退款。
  assert.equal(fields.get("Decision"), "APPROVE");
  assert.equal(fields.get("Continuation"), "SUCCEEDED");
  assert.ok(!fields.has("Refund applied"));
  assert.ok(![...fields.values()].some(value => value.includes("129.99")));

  // Closing and reopening reads the saved record again and still shows the saved reason.
  // 关闭后重新打开会再次读取已保存的记录，仍能看到已保存的失效原因。
  dialog.close();
  const reopened = await open();
  assert.equal(reads(), 2);
  assert.equal(reopened.get("Proposal invalidated"), ORDER_CHANGED_TEXT);
  assert.ok(!reopened.has("Refund applied"));
});

for (const [name, audit] of [
  ["a legacy record with null audit fields", { invalidation_reason: null, revalidated_at: null }],
  ["a legacy record without audit fields", {}],
  ["an unrecognized invalidation code", { invalidation_reason: "constructor", revalidated_at: null }],
]) {
  test(`${name} shows the generic invalidation message`, async () => {
    const { open } = executionDetailClient({
      order_id: "1001", outcome: "ELIGIBLE", reason_codes: [], status: "INVALIDATED",
      decision: null, resume_job_status: null, result: null, ...audit,
    });
    const fields = await open();
    assert.equal(fields.get("Proposal invalidated"), GENERIC_INVALIDATION_TEXT);
    assert.ok(!fields.has("Verified at"));
    assert.ok(!fields.has("Refund applied"));
  });
}

test("a completed refund shows the applied result amount, and a verification time alone is not invalidation", async () => {
  const { open } = executionDetailClient({
    order_id: "1001", outcome: "ELIGIBLE", reason_codes: [], refund_amount_minor: "12999", currency: "CAD",
    status: "COMPLETED", invalidation_reason: null, revalidated_at: "2026-11-21T15:30:00Z",
    decision: { decision: "APPROVE" }, resume_job_status: "SUCCEEDED",
    // The applied amount is read from the saved result, even when it differs from the proposal.
    // 已退款金额取自已保存的 result，即使它与方案金额不同。
    result: { refund_amount_minor: "9007199254740993", currency: "CAD" },
  });
  const fields = await open();
  assert.equal(fields.get("Refund applied"), "CAD 90071992547409.93");
  assert.ok(fields.has("Verified at"));
  assert.ok(!fields.has("Proposal invalidated"));
});

test("eligibility codes are labelled as eligibility reasons, separate from invalidation", async () => {
  const { open } = executionDetailClient({
    order_id: "1002", outcome: "INELIGIBLE", reason_codes: ["WINDOW_EXPIRED"], status: "COMPLETED",
    invalidation_reason: "ORDER_CHANGED", revalidated_at: null, decision: null, resume_job_status: null, result: null,
  });
  const fields = await open();
  assert.equal(fields.get("Eligibility reasons"), "WINDOW_EXPIRED");
  assert.ok(!fields.has("Reason"));
  // A stray code on a non-invalidated execution is not presented as invalidation.
  // 未失效的记录即使带有失效代码，也不会显示为方案失效。
  assert.ok(!fields.has("Proposal invalidated"));
});

test("a saved execution opens from history and displays only an applied refund as paid", async () => {
  let applied = false;
  const client = createClient(async url => {
    assert.equal(url, "/executions/ex-1");
    return response({ outcome: "ELIGIBLE", order_id: "1002", reason_codes: [], status: "COMPLETED",
      result: applied ? { refund_amount_minor: "9007199254740993", currency: "CAD" } : null });
  });
  const dialog = client.element("execution-panel");
  dialog.showModal = () => { dialog.open = true; };
  client.run("showMessages([{role:'user', content:'Refund', execution_id:'ex-1'}])");
  const click = client.element("messages").children[0].children[2].listeners.get("click");
  await click();
  const values = () => client.element("execution-fields").children.map(el => el.textContent);
  assert.ok(values().includes("ELIGIBLE"));
  assert.ok(!values().includes("Refund applied"));
  applied = true;
  await click();
  assert.ok(values().includes("CAD 90071992547409.93"));
});

test("an open refund dialog updates on SSE and ignores an older in-flight refresh", async t => {
  let stream, resolveQueued, detailReads = 0, historyReads = 0;
  let detail = { outcome: "ELIGIBLE", order_id: "1001", reason_codes: [], status: "WAITING_FOR_APPROVAL", result: null };
  const client = createClient(async (url, options) => {
    if (url === "/conversations/chat-a/events?version=2") {
      stream = sseResponse(options.signal);
      return stream.response;
    }
    if (url.includes("/messages?")) {
      historyReads++;
      return response([{ seq: 1, role: "user", content: "Refund", execution_id: "ex-1" }]);
    }
    if (url === "/conversations") return response([]);
    if (url === "/executions/ex-1") {
      const snapshot = response(detail);
      if (++detailReads === 2) return new Promise(resolve => { resolveQueued = () => resolve(snapshot); });
      return snapshot;
    }
    throw new Error("Unexpected request: " + url);
  }, streamingGlobals);
  t.after(() => client.run("stopStream()"));
  await client.run("authorization = 'Basic test'; openConversation({id:'chat-a', title:'A'})");
  await until(() => stream);
  stream.push('event: ready\ndata: {"cursor":"v2:0"}\n\n');
  await until(() => historyReads === 2);
  await client.element("messages").children[0].children[2].listeners.get("click")();
  const values = () => client.element("execution-fields").children.map(el => el.textContent);
  assert.ok(values().includes("WAITING_FOR_APPROVAL"));

  function push(seq, type) {
    stream.push(`id: v2:${seq}\nevent: ${type}\ndata: ` + JSON.stringify({
      seq: String(seq), type, conversation_id: "chat-a", payload: { proposal_id: "p1", execution_id: "ex-1" },
    }) + '\n\n');
  }
  detail = { ...detail, status: "QUEUED", decision: { decision: "APPROVE" }, resume_job_status: "QUEUED" };
  push(1, "proposal.decided");
  await until(() => resolveQueued);
  detail = { ...detail, status: "COMPLETED", resume_job_status: "SUCCEEDED",
    result: { refund_amount_minor: "12999", currency: "CAD" } };
  push(2, "resolution.completed");
  await until(() => values().includes("CAD 129.99"));
  resolveQueued();
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(client.element("execution-panel").open, true);
  assert.deepEqual(values(), ["Order", "1001", "Eligibility", "ELIGIBLE", "Workflow status", "COMPLETED",
    "Decision", "APPROVE", "Continuation", "SUCCEEDED", "Refund applied", "CAD 129.99"]);
  assert.equal(detailReads, 3);
});

// The live refresh path must render the saved invalidation reason and verification time, like the first load.
// 实时刷新与首次加载一样，必须显示已保存的失效原因和核验时间。
test("an open refund dialog shows a live invalidation with its saved reason and verification time", async t => {
  let stream, historyReads = 0;
  let detail = { outcome: "ELIGIBLE", order_id: "1001", reason_codes: [], status: "QUEUED", result: null,
    decision: { decision: "APPROVE" }, resume_job_status: "QUEUED", invalidation_reason: null, revalidated_at: null };
  const client = createClient(async (url, options) => {
    if (url === "/conversations/chat-a/events?version=2") {
      stream = sseResponse(options.signal);
      return stream.response;
    }
    if (url.includes("/messages?")) {
      historyReads++;
      return response([{ seq: 1, role: "user", content: "Refund", execution_id: "ex-1" }]);
    }
    if (url === "/conversations") return response([]);
    if (url === "/executions/ex-1") return response(detail);
    throw new Error("Unexpected request: " + url);
  }, streamingGlobals);
  t.after(() => client.run("stopStream()"));
  await client.run("authorization = 'Basic test'; openConversation({id:'chat-a', title:'A'})");
  await until(() => stream);
  stream.push('event: ready\ndata: {"cursor":"v2:0"}\n\n');
  await until(() => historyReads === 2);
  await client.element("messages").children[0].children[2].listeners.get("click")();
  const pairs = () => {
    const children = client.element("execution-fields").children;
    const fields = new Map();
    for (let i = 0; i < children.length; i += 2) fields.set(children[i].textContent, children[i + 1].textContent);
    return fields;
  };
  assert.equal(pairs().get("Workflow status"), "QUEUED");
  assert.ok(!pairs().has("Proposal invalidated"));

  // The approved continuation succeeds, but revalidation finds the order changed.
  // 已批准的后续任务成功，但重新核验发现订单已变化。
  detail = { ...detail, status: "INVALIDATED", resume_job_status: "SUCCEEDED",
    invalidation_reason: "ORDER_CHANGED", revalidated_at: "2026-11-23T10:00:00Z" };
  stream.push("id: v2:1\nevent: resolution.invalidated\ndata: " + JSON.stringify({
    seq: "1", type: "resolution.invalidated", conversation_id: "chat-a", payload: { execution_id: "ex-1" },
  }) + "\n\n");
  await until(() => pairs().get("Workflow status") === "INVALIDATED");
  assert.equal(pairs().get("Proposal invalidated"),
    "The order changed after the proposal was prepared, so it was not applied.");
  assert.equal(pairs().get("Verified at"), new Date("2026-11-23T10:00:00Z").toLocaleString());
  assert.equal(pairs().get("Continuation"), "SUCCEEDED");
  assert.ok(!pairs().has("Refund applied"));
});
