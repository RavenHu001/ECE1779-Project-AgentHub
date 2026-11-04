"use strict";

// Keep credentials and submission state in this page's memory; only theme preferences persist.
// 凭据与提交状态只保存在当前页面内存中，只有主题偏好会写入浏览器本地存储。
/** Resolve one stable element by its HTML id.
 * 根据 HTML id 查找对应的页面元素。
 */
const $ = id => document.getElementById(id);
let authorization = null;
let conversation = null;
let busy = false;
let generation = 0;
let messageRead = 0;
let pending = null;
let conversations = [];
let resumeAfterLogin = false;
const drafts = new Map();

/** Wait between job polls without blocking other browser interactions.
 * 在两次任务查询之间异步等待，不阻塞浏览器的其他交互。
 */
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

/** Call an authenticated same-origin endpoint with a bounded request timeout.
 * 携带内存中的认证信息调用同源接口，并限制单次请求的等待时间。
 */
async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { Authorization: authorization, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) {
    // Present safe English summaries instead of exposing raw server or provider errors.
    // 使用安全的英文错误摘要，不直接展示服务端或模型提供商的原始错误正文。
    if (response.status === 401) throw new Error("Please check your username and password, then sign in again.");
    throw new Error("Request failed (" + response.status + "). Please try again.");
  }
  return response.json();
}

/** Set or clear an accessible error notice in the workspace or sign-in dialog.
 * 更新或清空工作区、登录对话框中供辅助技术读取的错误提示。
 */
function notice(message = "", target = "notice") {
  $(target).textContent = message;
  $(target).hidden = !message;
}

/** Fit the composer to its content and enable sending only when input is usable.
 * 根据文本内容调整输入框高度，并仅在存在有效文本且空闲时启用发送按钮。
 */
function resizeComposer() {
  $("message").style.height = "auto";
  $("message").style.height = Math.min($("message").scrollHeight, 180) + "px";
  $("send").disabled = busy || !$("message").value.trim();
}

/** Disable actions that could change the active submission while polling is in progress.
 * 查询任务结果期间禁用会改变当前提交的操作，避免切换会话或重复发送。
 */
function setBusy(value) {
  busy = value;
  for (const id of ["message", "new-chat", "sign-out"]) $(id).disabled = value;
  document.querySelectorAll("#conversations button, .suggestions button").forEach(button => button.disabled = value);
  resizeComposer();
  $("message-form").setAttribute("aria-busy", String(value));
}

/** Show a concise job state and optionally animate the waiting indicator.
 * 显示简短任务状态，并按需启用等待动画。
 */
function status(text = "", working = false) {
  $("job-status").textContent = text;
  $("job-status").classList.toggle("working", working);
}

/** Replace message nodes with plain text and scroll to the newest visible reply.
 * 以纯文本替换消息节点，并滚动到最新的可见回复，避免把用户或模型文本当作 HTML。
 */
function showMessages(rows, waiting = false) {
  $("chat").classList.toggle("has-messages", rows.length > 0 || waiting);
  $("messages").replaceChildren();
  for (const row of rows) {
    const article = document.createElement("article");
    article.className = "message " + row.role;
    const label = document.createElement("strong");
    label.textContent = row.role === "user" ? "You" : "AgentHub";
    const text = document.createElement("p");
    // Never interpret user or model text as HTML.
    // 始终通过 textContent 写入用户与模型内容，避免执行文本中的 HTML。
    text.textContent = row.content;
    article.append(label, text);
    $("messages").append(article);
  }
  if (waiting) {
    // The waiting row is local presentation state, never a persisted assistant message.
    // 等待提示仅属于本地界面状态，不会作为助手消息写入数据库。
    const thinking = document.createElement("div");
    thinking.className = "pending-message";
    thinking.textContent = "Working on it";
    $("messages").append(thinking);
  }
  $("messages").scrollTop = $("messages").scrollHeight;
}

/** Filter loaded conversation titles and rebuild the selected, busy-aware navigation list.
 * 根据标题筛选已加载的会话，并重建包含选中状态与禁用状态的导航列表。
 */
function drawConversations() {
  const query = $("search").value.trim().toLowerCase();
  const rows = conversations.filter(row => row.title.toLowerCase().includes(query));
  $("conversations").replaceChildren();
  $("chat-count").textContent = conversations.length || "";
  for (const row of rows) {
    const button = document.createElement("button");
    button.textContent = row.title;
    button.title = row.title;
    button.setAttribute("aria-current", String(row.id === conversation));
    button.disabled = busy;
    button.addEventListener("click", () => openConversation(row).catch(error => notice(error.message)));
    $("conversations").append(button);
  }
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "history-empty";
    empty.textContent = query ? "No matching conversations." : authorization
      ? "Your next good idea starts here." : "Sign in to see your conversations.";
    $("conversations").append(empty);
  }
}

/** Refresh conversation metadata only if the authenticated account has not changed.
 * 刷新会话元数据；等待期间若认证账号已改变，则丢弃旧账号的查询结果。
 */
async function listConversations() {
  const activeAuth = authorization;
  const rows = await api("/conversations");
  if (activeAuth !== authorization) return;
  conversations = rows;
  drawConversations();
}

/** Read complete history, allowing only the latest request for the active chat to render.
 * 分页读取完整历史；只有当前会话中最新发起的读取请求才有权更新界面。
 */
async function renderMessages(waiting = false) {
  const activeConversation = conversation;
  const activeGeneration = generation;
  const request = ++messageRead;
  if (!activeConversation) return;

  /** Fence both navigation changes and overlapping reads within the same conversation.
   * 同时检查会话导航变化与同一会话内的并发读取，阻止旧响应覆盖新消息。
   */
  function isCurrent() {
    return activeConversation === conversation && activeGeneration === generation && request === messageRead;
  }

  let rows = [], after = 0;
  while (true) {
    let page;
    try {
      page = await api("/conversations/" + activeConversation + "/messages?after_seq=" + after + "&limit=500");
    } catch (error) {
      // A superseded failure must not replace the notice or status of a newer successful send.
      // 已失效请求的错误不能覆盖后续成功发送的提示或状态，当前请求失败才继续抛出。
      if (!isCurrent()) return;
      throw error;
    }
    // Stop stale pagination immediately, including after sign-out or a newer history refresh.
    // 退出登录、切换会话或新读取发起后，立即停止旧请求的后续分页，避免继续使用旧状态。
    if (!isCurrent()) return;
    rows.push(...page);
    if (page.length < 500) break;
    after = page[page.length - 1].seq;
  }
  if (isCurrent()) showMessages(rows, waiting);
}

/** Synchronize desktop/mobile sidebar visibility, keyboard reachability, and ARIA state.
 * 同步桌面与手机侧栏的可见性、键盘可达性和 ARIA 展开状态。
 */
function setSidebar(open) {
  const mobile = matchMedia("(max-width: 640px)").matches;
  $("workspace").classList.toggle("mobile-sidebar-open", mobile && open);
  $("workspace").classList.toggle("sidebar-collapsed", !mobile && !open);
  $("sidebar-overlay").hidden = !(mobile && open);
  $("sidebar").inert = !open;
  $("sidebar-toggle").setAttribute("aria-expanded", String(open));
}

/** Save a draft together with its uncertain submission key before leaving a saved chat.
 * 离开已保存的会话前，将草稿与响应尚未确认的提交标识一同保存到页面内存。
 */
function rememberDraft() {
  if (!authorization || !conversation) return;
  drafts.set(conversation, { content: $("message").value, pending });
}

/** Restore matching draft and retry identity; a different chat starts with its own input.
 * 同时恢复会话草稿与重试标识，保证不同会话的输入内容和去重状态不会混用。
 */
function restoreDraft(id) {
  const draft = drafts.get(id);
  $("message").value = draft?.content || "";
  pending = draft?.pending || null;
  resizeComposer();
}

/** Open an empty composer while retaining a saved chat's in-memory draft for later return.
 * 打开空白会话输入框，同时保留原有会话的内存草稿，便于返回时继续处理。
 */
function newChat() {
  if (busy) return;
  rememberDraft();
  generation++;
  pending = null;
  conversation = null;
  $("chat-title").textContent = "New chat";
  $("message").value = "";
  showMessages([]);
  status();
  notice();
  drawConversations();
  resizeComposer();
  if (matchMedia("(max-width: 640px)").matches) setSidebar(false);
  $("message").focus();
}

/** Open saved history without discarding the current chat's uncertain submission.
 * 打开已保存历史；重开当前会话时保留待确认提交，切换会话时成对保存和恢复草稿。
 */
async function openConversation(row) {
  if (busy) return;
  // Changing chats moves the draft and its key together; reopening one leaves both intact.
  // 切换会话时将草稿与标识一起迁移；重开当前会话则保持二者不变。
  if (row.id !== conversation) {
    rememberDraft();
    restoreDraft(row.id);
  }
  generation++;
  conversation = row.id;
  $("chat-title").textContent = row.title;
  status();
  notice();
  drawConversations();
  if (matchMedia("(max-width: 640px)").matches) setSidebar(false);
  await renderMessages();
}

/** Open the sign-in dialog without replacing a message the user already typed.
 * 打开登录对话框，并保留用户已经输入、准备在登录后发送的消息。
 */
function openLogin() {
  notice("", "login-error");
  if (!$("login-panel").open) $("login-panel").showModal();
}

// Authenticate first, then load the account's conversations and resume an interrupted send.
// 先验证身份，再加载该账号的会话；若登录由发送动作触发，则继续原来的提交。
$("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  notice("", "login-error");
  $("login-submit").disabled = true;
  try {
    // Basic credentials stay in memory; remove the password from the form after validation.
    // Basic 认证信息只驻留内存；验证成功后立即从表单中移除明文密码。
    authorization = "Basic " + btoa($("username").value + ":" + $("password").value);
    const account = await api("/me");
    $("password").value = "";
    $("account-name").textContent = account.display_name || account.username;
    $("account-action").textContent = "Sign out";
    $("sign-out").setAttribute("aria-label", "Sign out");
    $("avatar").textContent = (account.display_name || account.username).slice(0, 2).toUpperCase();
    await listConversations();
    $("login-panel").close();
    if (resumeAfterLogin) {
      resumeAfterLogin = false;
      $("message-form").requestSubmit();
    } else $("message").focus();
  } catch (error) {
    authorization = null;
    notice(error.message, "login-error");
  } finally { $("login-submit").disabled = false; }
});

// Closing by button, Escape, or backdrop cancels automatic submission and clears the password.
// 通过按钮、Escape 或遮罩关闭登录框时，取消自动提交并清除密码输入。
$("close-login").addEventListener("click", () => $("login-panel").close());
$("login-panel").addEventListener("close", () => { resumeAfterLogin = false; $("password").value = ""; });
$("login-panel").addEventListener("click", event => {
  if (event.target !== $("login-panel")) return;
  const rect = $("login-panel").getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)
    $("login-panel").close();
});

// Sign-out clears every account-scoped draft before returning to the unauthenticated workspace.
// 退出时先清除属于该账号的全部草稿，再回到未登录工作区，防止下个账号读取旧内容。
$("new-chat").addEventListener("click", newChat);
$("sign-out").addEventListener("click", () => {
  if (!authorization) { openLogin(); return; }
  authorization = null;
  conversations = [];
  drafts.clear();
  $("account-name").textContent = "Your workspace";
  $("account-action").textContent = "Sign in to get started";
  $("sign-out").setAttribute("aria-label", "Sign in");
  $("avatar").textContent = "A";
  newChat();
});

// Search filters loaded titles locally; sidebar controls preserve a usable keyboard focus.
// 搜索在本地筛选已加载标题；侧栏开关关闭后将键盘焦点移回可用的控制按钮。
$("search-toggle").addEventListener("click", () => {
  if (matchMedia("(max-width: 640px)").matches) setSidebar(true);
  $("search").hidden = !$("search").hidden;
  if (!$("search").hidden) $("search").focus();
  else { $("search").value = ""; drawConversations(); }
});
$("search").addEventListener("input", drawConversations);
$("sidebar-toggle").addEventListener("click", () => {
  const mobile = matchMedia("(max-width: 640px)").matches;
  setSidebar(mobile ? !$("workspace").classList.contains("mobile-sidebar-open") : $("workspace").classList.contains("sidebar-collapsed"));
});
$("close-sidebar").addEventListener("click", () => { setSidebar(false); $("sidebar-toggle").focus(); });
$("sidebar-overlay").addEventListener("click", () => { setSidebar(false); $("sidebar-toggle").focus(); });
matchMedia("(max-width: 640px)").addEventListener("change", () => setSidebar(!matchMedia("(max-width: 640px)").matches));

/** Apply one theme and update the toggle's accessible next-action description.
 * 应用主题，并同步切换按钮对辅助技术提供的下一步操作说明。
 */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("theme-toggle").setAttribute("aria-label", "Switch to " + (theme === "dark" ? "light" : "dark") + " theme");
  $("theme-toggle").setAttribute("aria-pressed", String(theme === "dark"));
}

// Appearance storage is optional; private-browsing restrictions must not break the page.
// 外观偏好存储属于可选能力，隐私模式等存储限制不能导致页面无法使用。
try { applyTheme(localStorage.getItem("agenthub-theme") === "dark" ? "dark" : "light"); }
catch { applyTheme("light"); }
$("theme-toggle").addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(theme);
  try { localStorage.setItem("agenthub-theme", theme); }
  catch { /* Optional preference storage. 偏好存储失败不影响聊天。 */ }
});

// Enter sends, Shift+Enter adds a line, and IME composition is never mistaken for a send.
// Enter 发送、Shift+Enter 换行；输入法正在组合文字时不把确认按键误判为发送。
$("message").addEventListener("input", resizeComposer);
$("message").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!busy && $("message").value.trim()) $("message-form").requestSubmit();
  }
});
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k" && !$("login-panel").open) {
    event.preventDefault(); newChat();
  }
  if (event.key === "Escape" && !$("login-panel").open && matchMedia("(max-width: 640px)").matches) setSidebar(false);
});
document.querySelectorAll("[data-prompt]").forEach(button => button.addEventListener("click", () => {
  $("message").value = button.dataset.prompt;
  resizeComposer();
  $("message").focus();
}));

// One logical message retains its key until success, terminal failure, or an explicit new draft.
// 同一条逻辑消息保留原去重标识，直到成功、确定失败，或用户明确改为新的输入。
$("message-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (busy || !$("message").value.trim()) return;
  if (!authorization) { resumeAfterLogin = true; openLogin(); return; }
  const content = $("message").value;
  const currentGeneration = generation;
  setBusy(true);
  notice();
  try {
    if (!conversation) {
      // Slice Unicode code points so the title never ends with half of an emoji.
      // 按 Unicode 码点截取标题，避免将 emoji 的代理对截成无法保存的半个字符。
      const title = Array.from(content.trim()).slice(0, 70).join("");
      const created = await api("/conversations", { title });
      conversation = created.id;
      $("chat-title").textContent = created.title;
    }
    // A network retry retains the logical submission key.
    // 网络错误导致是否接受未知时，重试必须沿用相同的逻辑提交标识。
    if (!pending || pending.content !== content || pending.conversation !== conversation)
      pending = { content, conversation, submission_key: crypto.randomUUID() };
    const receipt = await api("/conversations/" + conversation + "/messages", {
      content: pending.content, submission_key: pending.submission_key,
    });
    status("Queued", true);
    await renderMessages(true);
    await listConversations();
    // Poll the durable job; a browser timeout leaves its retry identity available in memory.
    // 查询持久任务状态；浏览器等待超时后仍在内存保留标识，以便继续确认同一任务。
    const deadline = Date.now() + 180000;
    while (Date.now() < deadline && generation === currentGeneration) {
      const job = await api("/jobs/" + receipt.job_id);
      status(job.status === "QUEUED" ? "Queued" : "Working", true);
      if (job.status === "FAILED") {
        pending = null;
        status("Failed");
        throw new Error(job.last_error || "The reply could not be completed.");
      }
      if (job.status === "SUCCEEDED") {
        pending = null;
        drafts.delete(conversation);
        $("message").value = "";
        status();
        await renderMessages();
        await listConversations();
        return;
      }
      await sleep(750);
    }
    throw new Error("Still waiting. Send the same text again to check this job without duplicating it.");
  } catch (error) {
    // Keep the text and uncertain key on transport failures so retrying cannot duplicate work.
    // 传输失败时保留原文及待确认标识，避免重试创建重复消息或后台任务。
    status("Needs attention");
    document.querySelector(".pending-message")?.remove();
    notice(error.message);
  } finally {
    setBusy(false);
    $("message").focus();
  }
});

// Health reports configured mode and database availability, not verified model readiness.
// 健康检查只报告配置模式和数据库可用性，不代表模型已成功调用或 Worker 正常工作。
fetch("/health", { signal: AbortSignal.timeout(10000) }).then(response => {
  if (!response.ok) throw new Error("API unavailable");
  return response.json();
}).then(health => {
  const fixed = health.reply_mode === "fixed";
  $("mode").textContent = fixed ? "Demo mode" : "Model chat";
  $("mode").classList.toggle("live", !fixed);
  $("mode").title = fixed ? "Fixed replies. No model API charges." : "Live model replies. API usage is billed.";
  $("composer-note").textContent = fixed
    ? "Demo mode · Fixed replies, no model charges." : "AgentHub can make mistakes. Review important information.";
}).catch(() => {
  $("mode").textContent = "Offline";
  $("mode").classList.add("offline");
  $("composer-note").textContent = "The server is unavailable. Check that the application is running.";
  $("connection-label").textContent = "Disconnected";
});

// Initialize responsive navigation and composer state after event handlers are registered.
// 注册事件处理函数后，再初始化响应式导航和输入框状态。
setSidebar(!matchMedia("(max-width: 640px)").matches);
drawConversations();
resizeComposer();
