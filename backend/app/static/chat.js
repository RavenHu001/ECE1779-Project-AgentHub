"use strict";

// The browser owns an HTTP-only session cookie; this page stores only account identity and drafts.
// 浏览器持有 HTTP-only 会话 cookie；页面仅保存账号标识和草稿。
/** Resolve one stable element by its HTML id.
 * 根据 HTML id 查找对应的页面元素。
 */
const $ = id => document.getElementById(id);
let authorization = null;
let accountId = null;
let authGeneration = 0;
let authBusy = false;
let conversation = null;
let busy = false;
let generation = 0;
let messageRead = 0;
let pending = null;
let conversations = [];
let conversationRead = 0;
let chatMenu = null;
let chatAction = null;
let chatMutation = false;
let resumeAfterLogin = false;
const drafts = new Map();
const pendingDecisions = new Set();
const watchedResolutions = new Set();
// Live updates: the latest streamed job states, their waiters, and the open conversation stream.
// 实时更新：最近推送的任务状态、等待这些状态的回调，以及当前会话的事件流。
const jobStates = new Map();
const jobWaiters = new Map();
let stream = null;
let refreshTimer = null;
let awaitingReply = false;
// Streaming needs fetch body streams; older or test environments fall back to polling.
// 实时流依赖 fetch 响应体流；不支持的旧环境或测试环境自动退回轮询。
const streamingSupported = typeof AbortController === "function" && typeof TextDecoderStream === "function";
const STREAM_IDLE_MS = 45000;

/** Wait between job polls without blocking other browser interactions.
 * 在两次任务查询之间异步等待，不阻塞浏览器的其他交互。
 */
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

/** Call an authenticated same-origin endpoint with a bounded request timeout.
 * 携带内存中的认证信息调用同源接口，并限制单次请求的等待时间。
 */
function authHeaders(credentials) {
  const headers = { "X-AgentHub-Request": "1" };
  if (typeof credentials === "string") headers.Authorization = credentials;
  else if (credentials?.id) headers["X-AgentHub-Account"] = credentials.id;
  return headers;
}

async function api(path, body, credentials = authorization, method = body === undefined ? "GET" : "POST") {
  const response = await fetch(path, {
    method,
    headers: { ...authHeaders(credentials), "Content-Type": "application/json" },
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) {
    // Present safe English summaries instead of exposing raw server or provider errors.
    // 使用安全的英文错误摘要，不直接展示服务端或模型提供商的原始错误正文。
    const message = response.status === 401
      ? "Please check your username and password, then sign in again."
      : response.status === 409
        ? "This conversation changed. Review its latest messages before trying again."
        : "Request failed (" + response.status + "). Please try again.";
    throw Object.assign(new Error(message), { status: response.status });
  }
  return response.status === 204 ? null : response.json();
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

/** Show the live-update connection state in the footer.
 * 在页脚显示实时更新连接状态。
 */
function connection(state = "") {
  // Keep the health check's offline notice until the page reloads.
  // 健康检查判定离线后保留离线提示，直到页面重新加载。
  if ($("mode").classList.contains("offline")) return;
  $("connection-label").textContent = { live: "Live updates", reconnecting: "Reconnecting…" }[state] || "Local workspace";
  const footer = $("connection-label").parentElement;
  footer?.classList?.toggle("live", state === "live");
  footer?.classList?.toggle("reconnecting", state === "reconnecting");
}

/** Order job states along the worker's lifecycle; each claim increments attempts.
 * QUEUED(n) = 2n, RUNNING(n) = 2n - 1, terminal states last, so a retry still moves forward.
 * 按 Worker 生命周期给任务状态排序；每次领取都会增加 attempts，因此重试后的状态仍然向前推进。
 */
function jobProgress(job) {
  if (job.status === "SUCCEEDED" || job.status === "FAILED") return Infinity;
  const attempts = Number(job.attempts) || 0;
  return job.status === "RUNNING" ? 2 * attempts - 1 : 2 * attempts;
}

/** Record a pushed or polled job state, ignoring older states, and wake its waiters.
 * 记录推送或轮询得到的任务状态，忽略较旧的状态，并唤醒等待该任务的调用方。
 */
function noteJob(job, jobId = job.id) {
  const known = jobStates.get(jobId);
  // A delayed response or stale cache entry must never move the displayed state backwards.
  // 延迟到达的响应或旧缓存不能让界面显示的状态倒退。
  if (known && jobProgress(known) > jobProgress(job)) return;
  jobStates.set(jobId, job);
  for (const wake of [...(jobWaiters.get(jobId) || [])]) wake(job);
}

/** Resolve with the next streamed state of one job, or null after a timeout.
 * 等待某个任务的下一次推送状态；超时则返回 null。
 */
function waitForJobEvent(jobId, ms) {
  return new Promise(resolve => {
    const waiters = jobWaiters.get(jobId) || new Set();
    jobWaiters.set(jobId, waiters);
    const done = job => {
      clearTimeout(timer);
      waiters.delete(done);
      if (!waiters.size) jobWaiters.delete(jobId);
      resolve(job);
    };
    const timer = setTimeout(() => done(null), ms);
    waiters.add(done);
  });
}

/** Return the next job state: pushed by the live stream when connected, otherwise polled.
 * 获取任务的下一个状态：实时流连接时由服务端推送，否则退回 REST 轮询。
 */
async function nextJob(jobId, lastStatus) {
  const activeGeneration = generation;
  const known = jobStates.get(jobId);
  if (known && known.status !== lastStatus) return known;
  if (streamLive()) {
    // A silent stream still gets a periodic REST confirmation, so a missed event cannot stall the page.
    // 即使实时流长时间无事件，也会定期用 REST 确认，避免漏掉事件导致页面停滞。
    const pushed = await waitForJobEvent(jobId, 5000);
    if (pushed) return pushed;
  } else if (lastStatus !== undefined) await sleep(750);
  // The first fallback check runs immediately, matching the original polling behaviour.
  // 后备轮询的第一次查询立即执行，与原有轮询行为一致。
  if (activeGeneration !== generation) return null;
  const polled = await api("/jobs/" + jobId);
  if (activeGeneration !== generation) return null;
  // Merge into the same cache so pushed and polled states share one forward-only view.
  // 合并到同一缓存，使推送与轮询的状态共享一个只进不退的视图。
  noteJob(polled, jobId);
  return jobStates.get(jobId);
}

/** Report whether the open conversation currently receives pushed updates.
 * 判断当前打开的会话是否正在接收服务端推送。
 */
function streamLive() {
  return Boolean(stream?.live && stream.conversation === conversation);
}

/** Refresh history once after a burst of pushed changes, reusing the stale-read guards.
 * A failed read retries with backoff, because a live stream may send only heartbeats afterwards.
 * 一批推送变化到达后只刷新一次历史，并复用防止旧响应覆盖的保护逻辑。
 * 读取失败时按退避重试，因为之后实时流可能只发送心跳，不会再触发刷新。
 */
function scheduleRefresh(delay = 150, attempt = 0) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    const current = stream;
    if (!current || current.conversation !== conversation) return;
    // Reread open refund details on SSE updates so the dialog reflects approvals from another tab.
    // 收到 SSE 更新时重新读取退款详情，让打开的弹窗反映其他标签页的审批结果。
    refreshExecutionDetail();
    try {
      await renderMessages(awaitingReply);
    } catch (error) {
      if (stream !== current || current.conversation !== conversation) return;
      // Rejected credentials need a new sign-in; retrying would only repeat the 401 every 10 seconds.
      // 凭据被拒绝时需要重新登录；继续重试只会每 10 秒重复收到 401。
      if (error.status === 401) { authExpired(current); return; }
      // Other client errors will not succeed on retry; transient 5xx, 429 and network errors back off.
      // 其他客户端错误重试也不会成功；临时的 5xx、429 与网络错误按退避重试。
      if (error.status >= 400 && error.status < 500 && error.status !== 429) { notice(error.message); return; }
      // A newer event or navigation cancels this retry through clearTimeout or the stream check.
      // 新事件或会话切换会通过 clearTimeout 或事件流检查取消本次重试。
      scheduleRefresh(Math.min(1000 * 2 ** attempt, 10000), attempt + 1);
      return;
    }
    if (stream === current) listConversations().catch(() => {});
  }, delay);
}

/** Stop live updates after rejected credentials and ask the user to sign in again.
 * The draft and conversation stay in memory; a successful sign-in restarts the stream.
 * 凭据被拒绝后停止实时更新，并提示用户重新登录；草稿与当前会话保留在内存中，登录成功后重新连接。
 */
function authExpired(current) {
  if (stream !== current) return;
  // stopStream also clears any pending history refresh retry.
  // stopStream 同时清除待执行的历史刷新重试。
  stopStream();
  authorization = null;
  notice("Your session has expired. Please sign in again.");
  openLogin();
}

/** Close the current stream; navigation and sign-out must not keep old account data flowing.
 * 关闭当前事件流；切换会话或退出登录后不能继续接收旧账号的数据。
 */
function stopStream() {
  if (stream) stream.controller.abort();
  stream = null;
  clearTimeout(refreshTimer);
  connection();
}

/** Retire only the current unavailable chat, releasing its pending send immediately.
 * 只清理当前已不可用的会话，并立即释放其待确认提交。
 */
function conversationUnavailable(conversationId, credentials) {
  if (conversation !== conversationId || authorization !== credentials) return;
  drafts.delete(conversationId);
  conversations = conversations.filter(row => row.id !== conversationId);
  awaitingReply = false;
  // The old submit's finally block is fenced out by resetChat's new generation.
  // resetChat 推进代次后，旧提交的 finally 不再执行，所以在此释放提交状态。
  // A separate metadata mutation still owns its busy state until it settles.
  // 独立的会话资料修改仍持有忙碌状态，直到其请求结束。
  if (!chatMutation) setBusy(false);
  resetChat();
  const resetGeneration = generation;
  notice("This chat was removed or is no longer available.");
  listConversations().catch(error => {
    if (generation === resetGeneration && authorization === credentials) notice(error.message);
  });
}

/** Open a stream for the current conversation unless one is already serving it.
 * 为当前会话打开事件流；若已有同一会话和账号的流在运行则保持不变。
 */
function startStream() {
  if (stream && stream.conversation === conversation && stream.authorization === authorization) return;
  stopStream();
  if (!streamingSupported || !authorization || !conversation) return;
  const current = { conversation, authorization, controller: new AbortController(), live: false, cursor: null };
  stream = current;
  runStream(current);
}

/** Keep one conversation stream connected, resuming from the last complete event after drops.
 * 保持单个会话事件流连接；断线后从最后完整处理的事件游标续传。
 */
async function runStream(current) {
  let delay = 1000;
  while (stream === current) {
    let failed = false;
    // Each attempt has its own abort so an idle connection can be dropped without stopping the stream.
    // 每次连接使用独立的中止控制，空闲超时只断开本次连接，不结束整个实时流。
    const attempt = new AbortController();
    const stop = () => attempt.abort();
    current.controller.signal.addEventListener("abort", stop);
    let idle = setTimeout(stop, STREAM_IDLE_MS);
    try {
      const headers = authHeaders(current.authorization);
      const base = "/conversations/" + current.conversation;
      if (current.cursor === null) {
        // Read a first-page snapshot before subscribing. The ready refresh loads full history.
        const snapshot = await fetch(base + "/messages?after_seq=0&limit=1", {
          headers, cache: "no-store", signal: attempt.signal,
        });
        if (stream !== current) return;
        if (snapshot.status === 401) { authExpired(current); return; }
        if (snapshot.status === 404) {
          // Another tab can remove the chat between the history read and this snapshot. Retire it like
          // a stream 404. Both the stream check and the cleanup's conversation and credential checks
          // stop a late 404 from clearing a chat opened since.
          // 其他标签页可能在历史读取与本次快照之间删除会话。与事件流 404 一样清理；
          // 事件流检查以及清理函数的会话与凭据检查，可防止迟到的 404 清除之后打开的会话。
          if (stream === current) conversationUnavailable(current.conversation, current.authorization);
          return;
        }
        if (!snapshot.ok) throw new Error("History snapshot unavailable");
        const cursor = snapshot.headers.get("X-Conversation-Event-Cursor");
        if (!/^v2:[0-9]+$/.test(cursor)) throw new Error("Missing history cursor");
        current.cursor = cursor;
      }
      headers["Last-Event-ID"] = current.cursor;
      const response = await fetch(base + "/events?version=2", {
        headers, cache: "no-store", signal: attempt.signal,
      });
      if (response.status === 422) {
        current.cursor = null;
        throw new Error("Refresh the history cursor");
      }
      if (response.status === 401) { authExpired(current); return; }
      if (response.status === 404) {
        // Deletion can happen while disconnected, so a missed event has the same cleanup path.
        // 断线期间也可能删除会话，漏掉删除事件时使用相同的清理路径。
        if (stream === current) conversationUnavailable(current.conversation, current.authorization);
        return;
      }
      if (!response.ok || !response.body) throw new Error("Live updates unavailable (" + response.status + ")");
      await readEvents(response.body, frame => {
        clearTimeout(idle);
        idle = setTimeout(stop, STREAM_IDLE_MS);
        if (handleFrame(current, frame) === false) failed = true;
      });
      if (!current.live) failed = true;
    } catch {
      failed = true;
    } finally {
      clearTimeout(idle);
      current.controller.signal.removeEventListener("abort", stop);
    }
    if (stream !== current) return;
    current.live = false;
    connection("reconnecting");
    // A normal server-side close reconnects quickly; failures back off with jitter.
    // 服务端正常关闭时快速重连；失败时按带抖动的指数退避重试。
    if (failed) {
      await sleep(delay + Math.random() * 500);
      delay = Math.min(delay * 2, 15000);
    } else {
      delay = 1000;
      await sleep(250);
    }
  }
}

/** Parse the SSE subset the server sends: event, id and single-line JSON data fields.
 * 解析服务端使用的 SSE 子集：event、id 以及单行 JSON 的 data 字段。
 */
async function readEvents(body, onFrame) {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value;
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const frame = { event: "message", id: null, data: [] };
      for (const line of block.split("\n")) {
        // Comment lines are heartbeats; they only reset the idle timer.
        // 注释行是心跳，只用于重置空闲计时。
        if (!line || line.startsWith(":")) { if (line) onFrame(null); continue; }
        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        let text = colon < 0 ? "" : line.slice(colon + 1);
        if (text.startsWith(" ")) text = text.slice(1);
        if (field === "event") frame.event = text;
        else if (field === "id") frame.id = text;
        else if (field === "data") frame.data.push(text);
      }
      if (frame.data.length) onFrame({ ...frame, data: frame.data.join("\n") });
    }
  }
}

/** Apply one pushed frame; returns false when the server reported a failure.
 * 处理单个推送帧；服务端报告故障时返回 false。
 */
function handleFrame(current, frame) {
  if (!frame || stream !== current) return true;
  let data;
  try { data = JSON.parse(frame.data); } catch { return true; }
  if (frame.event === "error") return false;
  if (frame.event === "ready") {
    current.live = true;
    connection("live");
    scheduleRefresh();
    return true;
  }
  if (frame.event === "conversation.deleted" && data.conversation_id === current.conversation) {
    // Share main's cleanup: drafts, sidebar row, pending send and composer state.
    // The stream === current check at the top of this function fences obsolete streams.
    // 复用 main 的共享清理：草稿、侧栏条目、待确认提交与输入框状态；
    // 本函数开头的 stream === current 检查会屏蔽已失效的事件流。
    conversationUnavailable(current.conversation, current.authorization);
    return true;
  }
  if (!/^v2:[0-9]+$/.test(frame.id) || data.conversation_id !== current.conversation ||
      frame.event !== data.type || !data.payload) return true;
  if (BigInt(frame.id.slice(3)) <= BigInt(current.cursor.slice(3))) return true;
  if (frame.event === "job.status_changed") noteJob(data.payload, data.payload.job_id);
  // History contains the saved message, approval, refund and agent activity projections.
  scheduleRefresh();
  current.cursor = frame.id;
  return true;
}

/** Save a decision, then refresh the card only if its conversation is still open.
 * 保存审批决定；仅在其会话仍处于打开状态时刷新方案卡片。
 */
async function decideProposal(proposal, decision, buttons, activeGeneration) {
  if (generation !== activeGeneration || pendingDecisions.has(proposal.id)) return;
  pendingDecisions.add(proposal.id);
  buttons.forEach(button => button.disabled = true);
  notice();
  let receipt;
  try {
    receipt = await api("/proposals/" + proposal.id + "/decision", { decision, version: proposal.version });
    if (generation === activeGeneration) await renderMessages(busy);
  } catch (error) {
    // A conflicting or uncertain response may still leave a committed decision to display.
    // 发生冲突或响应不确定时，服务端仍可能已有可展示的已提交决定。
    if (generation === activeGeneration) {
      await renderMessages(busy).catch(() => {
        // The receipt can keep an approved job observable even if the history read fails.
        // 即使历史读取失败，也可用已收到的批准回执继续跟踪待办。
        if (generation === activeGeneration && receipt?.resume_job_id) {
          watchResolution({ ...receipt, status: "QUEUED" });
        }
      });
      if (generation === activeGeneration) notice(error.message);
    }
  } finally {
    pendingDecisions.delete(proposal.id);
    buttons.forEach(button => button.disabled = false);
    // A refresh during the request may have rendered a fresh set of disabled buttons.
    // 请求期间的刷新可能已经渲染了一组新的禁用按钮。
    const selector = '.proposal-card[data-proposal-id="' + proposal.id + '"] .proposal-actions button';
    document.querySelectorAll(selector).forEach(button => button.disabled = false);
  }
}

/** Follow a saved continuation until it changes state or the user navigates away.
 * 跟踪已保存的恢复待办，直到状态变化或用户离开当前会话。
 */
async function watchResolution(proposal) {
  const activeGeneration = generation;
  const key = activeGeneration + ":" + proposal.resume_job_id;
  if (watchedResolutions.has(key)) return;
  watchedResolutions.add(key);
  try {
    const deadline = Date.now() + 180000;
    let lastStatus = proposal.status;
    while (generation === activeGeneration && Date.now() < deadline) {
      try {
        // Pushed job events arrive immediately; polling is only the fallback.
        // 推送的任务事件会立即到达；轮询仅作为后备方案。
        const job = await nextJob(proposal.resume_job_id, lastStatus);
        if (generation !== activeGeneration) return;
        if (job.status !== lastStatus) await renderMessages(busy);
        if (job.status === "SUCCEEDED" || job.status === "FAILED") return;
        lastStatus = job.status;
      } catch (error) {
        // Brief network or server failures should not hide a later terminal result.
        // 短暂网络或服务端故障不应让后续终态无法显示。
        if (error.status && error.status < 500 && error.status !== 429) throw error;
        await sleep(750);
      }
    }
    if (generation === activeGeneration) {
      notice("Still waiting for the refund result. Reopen this conversation to check again.");
    }
  } catch (error) {
    if (generation === activeGeneration) notice(error.message);
  } finally {
    watchedResolutions.delete(key);
  }
}

/** Render the saved refund proposal using plain text and its persistent identity.
 * 使用纯文本和持久化标识渲染已保存的退款方案。
 */
function renderProposal(proposal) {
  const card = document.createElement("article");
  card.className = "proposal-card";
  card.setAttribute("data-proposal-id", proposal.id);

  const heading = document.createElement("h3");
  heading.textContent = "Refund proposal";
  const state = document.createElement("span");
  state.className = "proposal-state";
  state.textContent = {
    WAITING_FOR_APPROVAL: "Waiting for approval",
    QUEUED: "Queued for execution",
    RUNNING: "Applying simulated refund",
    COMPLETED: "Completed",
    REJECTED: "Rejected",
    INVALIDATED: "No longer valid",
    FAILED: "Execution failed",
  }[proposal.status];

  // Split the decimal digits without converting BIGINT cents to a JavaScript number.
  // 直接拆分十进制数字，避免将 BIGINT 金额转换成 JavaScript 浮点数。
  const amount = formatRefund(proposal.refund_amount_minor, proposal.currency);
  const details = document.createElement("dl");
  for (const [name, value] of [
    ["Ticket", "#" + proposal.ticket_id],
    ["Order", "#" + proposal.order_id],
    ["Simulated refund", amount],
    ["Order delivered", new Date(proposal.delivered_at).toLocaleDateString()],
    ["Refund policy", proposal.policy_version],
    ["Proposed ticket status", proposal.proposed_ticket_status === "RESOLVED" ? "Resolved" : proposal.proposed_ticket_status],
  ]) {
    const label = document.createElement("dt");
    label.textContent = name;
    const description = document.createElement("dd");
    description.textContent = value;
    details.append(label, description);
  }

  // Render the exact saved draft as text, even when its contents look like markup.
  // 原样以纯文本显示已保存的草稿，即使其中的内容看起来像 HTML。
  const draftLabel = document.createElement("h4");
  draftLabel.textContent = "Proposed customer reply";
  const draft = document.createElement("blockquote");
  draft.textContent = proposal.customer_reply;
  const expiry = document.createElement("p");
  expiry.className = "proposal-expiry";
  expiry.textContent = "Approval window ends " + new Date(proposal.expires_at).toLocaleString();
  card.append(heading, state, details, draftLabel, draft);
  if (proposal.status === "WAITING_FOR_APPROVAL") {
    // Only a pending proposal exposes human decision controls.
    // 只有待审批方案展示人工审批操作。
    const actions = document.createElement("div");
    actions.className = "proposal-actions";
    const cardGeneration = generation;
    const buttons = [];
    for (const [decision, label] of [["APPROVE", "Approve"], ["REJECT", "Reject"]]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = decision === "APPROVE" ? "proposal-approve" : "proposal-reject";
      button.textContent = label;
      button.disabled = pendingDecisions.has(proposal.id);
      button.addEventListener("click", () => decideProposal(proposal, decision, buttons, cardGeneration));
      buttons.push(button);
    }
    actions.append(...buttons);
    card.append(expiry, actions);
  }
  return card;
}

function formatRefund(minor, currency) {
  const digits = minor.padStart(3, "0");
  return currency + " " + digits.slice(0, -2) + "." + digits.slice(-2);
}

// Plain-English explanations for saved proposal invalidation codes.
// 已保存的方案失效原因代码对应的英文说明。
const INVALIDATION_MESSAGES = {
  PROPOSAL_EXPIRED: "The approval window ended before the refund could be applied.",
  ORDER_CHANGED: "The order changed after the proposal was prepared, so it was not applied.",
  POLICY_CHANGED: "The refund policy changed after the proposal was prepared, so it was not applied.",
};
const GENERIC_INVALIDATION = "The proposal is no longer valid, so it was not applied.";

/** Explain why a proposal was invalidated; missing or unknown codes get a generic message.
 * 说明方案失效的原因；原因缺失或无法识别时显示通用提示。
 */
function invalidationMessage(code) {
  // Object.hasOwn keeps inherited names such as "constructor" from matching.
  // Object.hasOwn 防止 "constructor" 等继承属性被误认为已知代码。
  return code && Object.hasOwn(INVALIDATION_MESSAGES, code) ? INVALIDATION_MESSAGES[code] : GENERIC_INVALIDATION;
}

let executionDetailId = null;
let detailRequest = 0;

function closeExecutionDetail() {
  executionDetailId = null;
  detailRequest++;
  if ($("execution-panel").open) $("execution-panel").close();
  $("execution-fields").replaceChildren();
}

function openExecutionDetail(id) {
  executionDetailId = id;
  const panel = $("execution-panel");
  $("execution-fields").replaceChildren();
  $("execution-status").textContent = "Loading…";
  if (!panel.open) panel.showModal();
  return refreshExecutionDetail();
}

/** Refresh the selected execution without clearing the visible fields while it loads.
 * 刷新当前查看的执行记录，加载期间保留已显示的详情。
 */
async function refreshExecutionDetail() {
  const panel = $("execution-panel");
  if (!panel.open || executionDetailId === null) return;
  const request = ++detailRequest;
  try {
    const detail = await api("/executions/" + encodeURIComponent(executionDetailId));
    // Only the latest request ID may update the open dialog, so late responses cannot restore stale details.
    // 只接受编号仍为最新且弹窗仍打开的响应，防止迟到的旧详情覆盖最新状态。
    if (request !== detailRequest || !panel.open) return;
    const entries = [
      ["Order", detail.order_id],
      ["Eligibility", detail.outcome],
      // These are the original eligibility codes, not the reason a proposal was later invalidated.
      // 这些是最初的资格判断代码，并非方案之后失效的原因。
      ["Eligibility reasons", detail.reason_codes.join(", ")],
      ["Workflow status", detail.status],
      // Only the INVALIDATED status means invalidation; a verification time alone does not.
      // 只有 INVALIDATED 状态才表示方案失效；仅有核验时间并不代表失效。
      ["Proposal invalidated", detail.status === "INVALIDATED" ? invalidationMessage(detail.invalidation_reason) : null],
      ["Verified at", detail.revalidated_at ? new Date(detail.revalidated_at).toLocaleString() : null],
      ["Decision", detail.decision?.decision],
      ["Continuation", detail.resume_job_status],
    ];
    // An approved proposal can be invalidated even after its continuation succeeds, so only a
    // saved result proves a refund. The amount comes from that result, not from the proposal.
    // 已批准的方案即使后续任务成功也可能失效，因此只有已保存的 result 才能证明已退款；
    // 金额取自 result，而不是方案中的金额。
    if (detail.result) entries.push(["Refund applied",
      formatRefund(detail.result.refund_amount_minor, detail.result.currency)]);
    const fields = [];
    for (const [name, value] of entries) {
      if (!value) continue;
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = name;
      description.textContent = value;
      fields.push(term, description);
    }
    $("execution-fields").replaceChildren(...fields);
    $("execution-status").textContent = "";
  } catch (error) {
    if (request === detailRequest && panel.open) $("execution-status").textContent = error.message;
  }
}

/** Replace history with plain text messages and any proposals attached to their sources.
 * 以纯文本消息及其来源消息所附带的方案重建历史。
 */
function showMessages(rows, waiting = false) {
  const pane = $("messages");
  const follow = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 80;
  const previousScroll = pane.scrollTop;
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
    if (row.execution_id) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "View refund details";
      button.addEventListener("click", () => openExecutionDetail(row.execution_id));
      article.append(button);
    }
    $("messages").append(article);
    if (row.activity) $("messages").append(renderActivity(row.activity, row.proposal));
    // Rebuild cards from the same history snapshot as their originating messages.
    // 方案卡片和来源消息使用同一份历史快照进行重建。
    if (row.proposal) {
      $("messages").append(renderProposal(row.proposal));
      if (row.proposal.status === "QUEUED" || row.proposal.status === "RUNNING") watchResolution(row.proposal);
    }
  }
  if (waiting && !rows.some(row => row.activity && ["QUEUED", "RUNNING"].includes(row.activity.status))) {
    // The waiting row is local presentation state, never a persisted assistant message.
    // 等待提示仅属于本地界面状态，不会作为助手消息写入数据库。
    const thinking = document.createElement("div");
    thinking.className = "pending-message";
    thinking.textContent = "Working on it";
    $("messages").append(thinking);
  }
  pane.scrollTop = follow || previousScroll === 0 ? pane.scrollHeight : previousScroll;
}

/** Show saved tool activity separately from the assistant's answer, using text only.
 * 用纯文本独立显示已保存的工具进度，与助手答复区分。
 */
function renderActivity(activity, proposal) {
  const panel = document.createElement("section");
  panel.className = "execution-activity";
  panel.setAttribute("aria-label", "Agent activity");
  const list = document.createElement("ol");
  const add = (text, state) => {
    const item = document.createElement("li");
    item.className = "activity-" + state;
    const mark = document.createElement("span");
    mark.className = "activity-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = { completed: "✓", running: "◌", waiting: "○", stopped: "!" }[state];
    const label = document.createElement("span");
    label.textContent = text;
    item.append(mark, label);
    list.append(item);
  };
  const active = ["QUEUED", "RUNNING"].includes(activity.status);
  for (const step of activity.steps) {
    const state = step.status === "running" ? (active ? (activity.status === "QUEUED" ? "waiting" : "running") : "stopped") : step.status;
    add(step.text + (state === "stopped" ? " — not completed" : ""), state);
  }
  if (activity.attempts > 1) add("Processing attempt " + activity.attempts + " — saved progress retained", "waiting");
  if (activity.status === "QUEUED") add(activity.attempts ? "Temporary interruption — retry scheduled" : "Queued", "waiting");
  else if (activity.status === "RUNNING") add("Agent working", "running");
  else if (activity.status === "FAILED") add("Execution stopped before completion", "stopped");
  const outcome = {
    WAITING_FOR_APPROVAL: ["Waiting for your approval — no refund applied", "waiting"],
    QUEUED: ["Approval saved — execution queued", "waiting"],
    RUNNING: ["Rechecking current facts and applying approved resolution", "running"],
    COMPLETED: ["Simulated refund applied and ticket resolved", "completed"],
    REJECTED: ["Proposal rejected — no refund applied", "stopped"],
    INVALIDATED: ["Proposal no longer valid — prepare a new proposal for approval", "stopped"],
    FAILED: ["Approved execution stopped before completion", "stopped"],
  }[proposal?.status];
  if (outcome) add(...outcome);
  panel.hidden = list.children.length === 0;
  panel.append(list);
  return panel;
}

/** Keep one menu bound to its chat, including when opened by keyboard or touch.
 * 鼠标、键盘与触屏共用一个菜单，并始终绑定正确的会话。
 */
function closeChatMenu(restoreFocus = false) {
  if (restoreFocus) chatMenu?.anchor.focus();
  chatMenu = null;
  $("chat-menu").hidden = true;
}

function openChatMenu(row, anchor, x, y) {
  if (busy || !authorization) return;
  closeChatMenu();
  chatMenu = { row, anchor };
  $("chat-pin").textContent = row.is_pinned ? "Unpin" : "Pin";
  const rect = anchor.getBoundingClientRect();
  const menu = $("chat-menu");
  menu.hidden = false;
  menu.style.left = Math.max(8, Math.min(x ?? rect.right, window.innerWidth - menu.offsetWidth - 8)) + "px";
  menu.style.top = Math.max(8, Math.min(y ?? rect.bottom, window.innerHeight - menu.offsetHeight - 8)) + "px";
  $("chat-rename").focus();
}

function closeChatAction() {
  if ($("chat-action-dialog").open) $("chat-action-dialog").close();
  chatAction = null;
}

function openChatAction(kind) {
  if (!chatMenu || busy) return;
  const row = chatMenu.row;
  closeChatMenu(true);
  chatAction = { kind, row, auth: authorization };
  const deleting = kind === "delete";
  $("chat-action-title").textContent = deleting ? "Delete chat?" : "Rename chat";
  $("chat-action-description").textContent = deleting
    ? 'Remove "' + row.title + '" from your chat history? Refund and audit records will be retained. This cannot be undone in the app.'
    : "Choose a name for this conversation.";
  $("chat-name-field").hidden = deleting;
  $("chat-name").disabled = deleting;
  $("chat-name").value = row.title;
  $("chat-action-submit").textContent = deleting ? "Delete" : "Save";
  $("chat-action-submit").classList.toggle("delete-confirm", deleting);
  notice("", "chat-action-error");
  $("chat-action-dialog").showModal();
  $(deleting ? "chat-action-cancel" : "chat-name").focus();
}

/** Persist explicit values, fence account changes, and refresh the authoritative list.
 * 保存明确值、隔离账号切换，并刷新权威会话列表。
 */
async function changeChat(row, kind, value) {
  if (busy || chatMutation || !authorization) return;
  const activeAuth = authorization;
  chatMutation = true;
  ++conversationRead;
  setBusy(true);
  $("chat-action-submit").disabled = true;
  $("chat-action-cancel").disabled = true;
  notice();
  try {
    const body = kind === "rename" ? { title: value } : kind === "pin" ? { is_pinned: value } : undefined;
    const updated = await api("/conversations/" + row.id, body, activeAuth, kind === "delete" ? "DELETE" : "PATCH");
    if (authorization !== activeAuth) return;
    if (kind === "delete") {
      drafts.delete(row.id);
      conversations = conversations.filter(item => item.id !== row.id);
      if (conversation === row.id) resetChat();
    } else {
      conversations = conversations.map(item => item.id === row.id ? updated : item);
      if (conversation === row.id) $("chat-title").textContent = updated.title;
    }
    closeChatAction();
    drawConversations();
    await listConversations();
  } catch (error) {
    if (authorization !== activeAuth) return;
    const message = kind === "delete" && error.status === 409
      ? "Finish active work or reject the pending proposal before deleting this chat."
      : error.message;
    notice(message, $("chat-action-dialog").open ? "chat-action-error" : "notice");
  } finally {
    chatMutation = false;
    if (authorization === activeAuth) setBusy(false);
    $("chat-action-submit").disabled = false;
    $("chat-action-cancel").disabled = false;
  }
}

$("chat-rename").addEventListener("click", () => openChatAction("rename"));
$("chat-delete").addEventListener("click", () => openChatAction("delete"));
$("chat-pin").addEventListener("click", () => {
  if (!chatMenu) return;
  const row = chatMenu.row;
  closeChatMenu(true);
  return changeChat(row, "pin", !row.is_pinned);
});
$("chat-action-cancel").addEventListener("click", closeChatAction);
$("chat-action-dialog").addEventListener("cancel", event => {
  if (chatMutation) event.preventDefault();
  else chatAction = null;
});
$("chat-action-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!chatAction || chatAction.auth !== authorization) return;
  const title = $("chat-name").value.trim();
  if (chatAction.kind === "rename" && (!title || [...title].length > 200)) {
    notice("Enter a chat name between 1 and 200 characters.", "chat-action-error");
    return;
  }
  await changeChat(chatAction.row, chatAction.kind, title);
});
document.addEventListener("pointerdown", event => {
  if (chatMenu && !$("chat-menu").contains(event.target) && !chatMenu.anchor.contains(event.target)) closeChatMenu();
});
$("conversations").addEventListener("scroll", () => closeChatMenu());
$("chat-menu").addEventListener("keydown", event => {
  const items = [$("chat-rename"), $("chat-pin"), $("chat-delete")];
  const index = items.indexOf(event.target);
  if (event.key === "Escape") { event.preventDefault(); closeChatMenu(true); }
  else if (event.key === "Tab") closeChatMenu();
  else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? 2 : (index + (event.key === "ArrowDown" ? 1 : 2)) % 3;
    items[next].focus();
  }
});

/** Filter loaded conversation titles and rebuild the selected, busy-aware navigation list.
 * 根据标题筛选已加载的会话，并重建包含选中状态与禁用状态的导航列表。
 */
function drawConversations() {
  closeChatMenu();
  const query = $("search").value.trim().toLowerCase();
  const rows = conversations.filter(row => row.title.toLowerCase().includes(query));
  $("conversations").replaceChildren();
  $("chat-count").textContent = conversations.length || "";
  for (const row of rows) {
    const container = document.createElement("div");
    container.className = "conversation-row";
    const button = document.createElement("button");
    button.className = "conversation-open";
    button.textContent = (row.is_pinned ? "📌 " : "") + row.title;
    button.title = row.title;
    button.setAttribute("aria-current", String(row.id === conversation));
    button.disabled = busy;
    button.addEventListener("click", () => openConversation(row).catch(error => notice(error.message)));
    const more = document.createElement("button");
    more.className = "conversation-more";
    more.textContent = "⋯";
    more.setAttribute("aria-label", "Actions for " + row.title);
    more.setAttribute("aria-haspopup", "menu");
    more.disabled = busy;
    more.addEventListener("click", () => openChatMenu(row, more));
    container.addEventListener("contextmenu", event => {
      event.preventDefault();
      openChatMenu(row, more, event.clientX, event.clientY);
    });
    button.addEventListener("keydown", event => {
      if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
        event.preventDefault();
        openChatMenu(row, more);
      }
    });
    container.append(button, more);
    $("conversations").append(container);
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
  const request = ++conversationRead;
  const rows = await api("/conversations");
  if (activeAuth !== authorization || request !== conversationRead) return;
  conversations = rows;
  drawConversations();
}

/** Read complete history, allowing only the latest request for the active chat to render.
 * 分页读取完整历史；只有当前会话中最新发起的读取请求才有权更新界面。
 */
async function renderMessages(waiting = false) {
  const activeConversation = conversation;
  const activeGeneration = generation;
  const credentials = authorization;
  const request = ++messageRead;
  if (!activeConversation) return;

  /** Fence both navigation changes and overlapping reads within the same conversation.
   * 同时检查会话导航变化与同一会话内的并发读取，阻止旧响应覆盖新消息。
   */
  function isCurrent() {
    return activeConversation === conversation && activeGeneration === generation
      && credentials === authorization && request === messageRead;
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
      if (error.status === 404) {
        conversationUnavailable(activeConversation, credentials);
        return;
      }
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
  if (!conversation) return;
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

/** Open an empty composer and invalidate outstanding reads for the previous chat.
 * 打开空白会话输入框，并使上一个会话尚未完成的读取失效。
 */
function resetChat() {
  closeChatMenu();
  closeChatAction();
  closeExecutionDetail();
  generation++;
  pending = null;
  conversation = null;
  stopStream();
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

/** Start a new chat, retaining the previous chat's draft for this account.
 * 开始新会话，并为当前账号保留上一个会话的草稿。
 */
function newChat() {
  if (busy) return;
  rememberDraft();
  resetChat();
}

/** Discard the previous account's workspace, including during an in-flight send.
 * 清除上一个账号的工作区，即使还有未完成的发送请求。
 */
function clearAccountWorkspace() {
  conversations = [];
  drafts.clear();
  jobStates.clear();
  resumeAfterLogin = false;
  awaitingReply = false;
  $("search").value = "";
  setBusy(false);
  resetChat();
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
    // Clear the previous conversation's controls before the new history request starts.
    // 发起新会话的历史请求前，清除上一会话中可操作的控件。
    showMessages([]);
  }
  closeExecutionDetail();
  generation++;
  conversation = row.id;
  startStream();
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
  closeExecutionDetail();
  notice("", "login-error");
  if (!$("login-panel").open) $("login-panel").showModal();
}

// Authenticate first, then load the account's conversations and resume an interrupted send.
// 先验证身份，再加载该账号的会话；若登录由发送动作触发，则继续原来的提交。
$("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (authBusy) return;
  authBusy = true;
  const attempt = ++authGeneration;
  notice("", "login-error");
  $("login-submit").disabled = true;
  try {
    const account = await api("/auth/login", {
      username: $("username").value, password: $("password").value,
    }, null);
    if (attempt !== authGeneration) return;
    activateAccount(account);
    await listConversations();
    $("login-panel").close();
    // Resume live updates for a conversation left open when the previous session expired.
    // 若上次会话过期时仍打开着某个会话，登录后恢复其实时更新。
    if (conversation) { notice(); startStream(); }
    if (resumeAfterLogin) {
      resumeAfterLogin = false;
      $("message-form").requestSubmit();
    } else $("message").focus();
  } catch (error) {
    notice(error.message, "login-error");
  } finally { authBusy = false; $("login-submit").disabled = false; }
});

function activateAccount(account) {
  // Each login gets a new identity object so late reads cannot replace a newer session's UI.
  // 每次登录创建新的标识对象，防止旧请求覆盖新会话界面。
  authorization = { id: account.id };
  if (accountId !== null && account.id !== accountId) clearAccountWorkspace();
  accountId = account.id;
  $("password").value = "";
  $("account-name").textContent = account.display_name || account.username;
  $("account-action").textContent = "Sign out";
  $("sign-out").setAttribute("aria-label", "Sign out");
  $("avatar").textContent = (account.display_name || account.username).slice(0, 2).toUpperCase();
}

async function restoreSession() {
  const attempt = authGeneration;
  try {
    const account = await api("/me", undefined, null);
    if (attempt !== authGeneration || authorization) return;
    activateAccount(account);
    await listConversations();
  } catch (error) {
    if (attempt === authGeneration && error.status !== 401) notice("Could not restore your session. Please sign in.");
  }
}

// Keep backdrop interactions separate from dismissal so selecting text cannot close the dialog.
// 遮罩交互不负责关闭弹窗，避免拖选文字时误关闭。
const loginPanel = $("login-panel");
$("close-login").addEventListener("click", () => loginPanel.close());
loginPanel.addEventListener("close", () => {
  resumeAfterLogin = false;
  $("password").value = "";
});

// Sign-out clears every account-scoped draft before returning to the unauthenticated workspace.
// 退出时先清除属于该账号的全部草稿，再回到未登录工作区，防止下个账号读取旧内容。
$("new-chat").addEventListener("click", newChat);
$("sign-out").addEventListener("click", async () => {
  if (!authorization && accountId === null) { openLogin(); return; }
  if (authBusy) return;
  authBusy = true;
  ++authGeneration;
  $("sign-out").disabled = true;
  stopStream();
  try {
    await api("/auth/logout", {}, authorization);
  } catch {
    notice("Sign-out could not be completed. Please try again.");
    if (conversation) startStream();
    return;
  } finally { authBusy = false; $("sign-out").disabled = false; }
  authorization = null;
  accountId = null;
  $("account-name").textContent = "Your workspace";
  $("account-action").textContent = "Sign in to get started";
  $("sign-out").setAttribute("aria-label", "Sign in");
  $("avatar").textContent = "A";
  clearAccountWorkspace();
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
      if (generation !== currentGeneration) return;
      conversation = created.id;
      $("chat-title").textContent = created.title;
      startStream();
    }
    // A network retry retains the logical submission key.
    // 网络错误导致是否接受未知时，重试必须沿用相同的逻辑提交标识。
    if (!pending || pending.content !== content || pending.conversation !== conversation)
      pending = { content, conversation, submission_key: crypto.randomUUID() };
    const receipt = await api("/conversations/" + conversation + "/messages", {
      content: pending.content, submission_key: pending.submission_key,
    });
    if (generation !== currentGeneration) return;
    status("Queued", true);
    awaitingReply = true;
    await renderMessages(true);
    if (generation !== currentGeneration) return;
    await listConversations();
    // Follow the durable job through pushed events, or by polling when live updates are unavailable.
    // A browser timeout leaves its retry identity available in memory.
    // 通过推送事件跟踪持久任务，实时更新不可用时退回轮询；浏览器等待超时后仍在内存保留标识。
    const deadline = Date.now() + 180000;
    let lastStatus;
    while (Date.now() < deadline && generation === currentGeneration) {
      const job = await nextJob(receipt.job_id, lastStatus);
      if (generation !== currentGeneration) return;
      lastStatus = job.status;
      status(job.status === "QUEUED" ? "Queued" : "Working", true);
      if (job.status === "FAILED") {
        awaitingReply = false;
        pending = null;
        status("Failed");
        const credentials = authorization;
        await renderMessages();
        if (generation !== currentGeneration || credentials !== authorization) return;
        let reason = job.last_error;
        // Durable events carry job status. Read the saved reason from the job endpoint on failure.
        // 持久化事件携带任务状态。任务失败时，从任务接口读取已保存的原因。
        if (reason === undefined) {
          const failedJob = await api("/jobs/" + receipt.job_id).catch(() => null);
          reason = failedJob?.last_error;
        }
        if (credentials !== authorization) return;
        throw new Error(reason || "The reply could not be completed.");
      }
      if (job.status === "SUCCEEDED") {
        awaitingReply = false;
        pending = null;
        drafts.delete(conversation);
        $("message").value = "";
        status();
        await renderMessages();
        if (generation === currentGeneration) await listConversations();
        return;
      }
      if (!streamLive()) await renderMessages(true);
    }
    throw new Error("Still waiting. Send the same text again to check this job without duplicating it.");
  } catch (error) {
    if (generation !== currentGeneration) return;
    // Keep the text and uncertain key on transport failures so retrying cannot duplicate work.
    // 传输失败时保留原文及待确认标识，避免重试创建重复消息或后台任务。
    awaitingReply = false;
    status("Needs attention");
    document.querySelector(".pending-message")?.remove();
    // Another tab may have paused this conversation with a newly saved proposal.
    // 另一标签页可能刚保存了方案，并因此暂停了当前会话。
    if (error.status === 409 && conversation) await renderMessages().catch(() => {});
    if (generation === currentGeneration) notice(error.message);
  } finally {
    if (generation === currentGeneration) {
      setBusy(false);
      $("message").focus();
    }
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
restoreSession();
