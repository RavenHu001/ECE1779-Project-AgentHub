"use strict";
const $ = id => document.getElementById(id);
let authorization = null;
let conversation = null;
let busy = false;
let generation = 0;
let pending = null;
let conversations = [];
let resumeAfterLogin = false;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { Authorization: authorization, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) {
    if (response.status === 401) throw new Error("Please check your username and password, then sign in again.");
    throw new Error("Request failed (" + response.status + "). Please try again.");
  }
  return response.json();
}

function notice(message = "", target = "notice") {
  $(target).textContent = message;
  $(target).hidden = !message;
}

function resizeComposer() {
  $("message").style.height = "auto";
  $("message").style.height = Math.min($("message").scrollHeight, 180) + "px";
  $("send").disabled = busy || !$("message").value.trim();
}

function setBusy(value) {
  busy = value;
  for (const id of ["message", "new-chat", "sign-out"]) $(id).disabled = value;
  document.querySelectorAll("#conversations button, .suggestions button").forEach(button => button.disabled = value);
  resizeComposer();
  $("message-form").setAttribute("aria-busy", String(value));
}

function status(text = "", working = false) {
  $("job-status").textContent = text;
  $("job-status").classList.toggle("working", working);
}

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
    text.textContent = row.content;
    article.append(label, text);
    $("messages").append(article);
  }
  if (waiting) {
    const thinking = document.createElement("div");
    thinking.className = "pending-message";
    thinking.textContent = "Working on it";
    $("messages").append(thinking);
  }
  $("messages").scrollTop = $("messages").scrollHeight;
}

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

async function listConversations() {
  const activeAuth = authorization;
  const rows = await api("/conversations");
  if (activeAuth !== authorization) return;
  conversations = rows;
  drawConversations();
}

async function renderMessages(waiting = false) {
  const activeConversation = conversation;
  const activeGeneration = generation;
  if (!activeConversation) return;
  let rows = [], after = 0;
  while (true) {
    const page = await api("/conversations/" + activeConversation + "/messages?after_seq=" + after + "&limit=500");
    rows.push(...page);
    if (page.length < 500) break;
    after = page[page.length - 1].seq;
  }
  if (activeConversation === conversation && activeGeneration === generation) showMessages(rows, waiting);
}

function setSidebar(open) {
  const mobile = matchMedia("(max-width: 640px)").matches;
  $("workspace").classList.toggle("mobile-sidebar-open", mobile && open);
  $("workspace").classList.toggle("sidebar-collapsed", !mobile && !open);
  $("sidebar-overlay").hidden = !(mobile && open);
  $("sidebar").inert = mobile && !open;
  $("sidebar-toggle").setAttribute("aria-expanded", String(open));
}

function newChat() {
  if (busy) return;
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

async function openConversation(row) {
  if (busy) return;
  generation++;
  pending = null;
  conversation = row.id;
  $("chat-title").textContent = row.title;
  status();
  notice();
  drawConversations();
  if (matchMedia("(max-width: 640px)").matches) setSidebar(false);
  await renderMessages();
}

function openLogin() {
  notice("", "login-error");
  if (!$("login-panel").open) $("login-panel").showModal();
}

$("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  notice("", "login-error");
  $("login-submit").disabled = true;
  try {
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

$("close-login").addEventListener("click", () => $("login-panel").close());
$("login-panel").addEventListener("close", () => { resumeAfterLogin = false; $("password").value = ""; });
$("login-panel").addEventListener("click", event => {
  if (event.target !== $("login-panel")) return;
  const rect = $("login-panel").getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)
    $("login-panel").close();
});

$("new-chat").addEventListener("click", newChat);
$("sign-out").addEventListener("click", () => {
  if (!authorization) { openLogin(); return; }
  authorization = null;
  conversations = [];
  $("account-name").textContent = "Your workspace";
  $("account-action").textContent = "Sign in to get started";
  $("sign-out").setAttribute("aria-label", "Sign in");
  $("avatar").textContent = "A";
  newChat();
});

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
$("sidebar-overlay").addEventListener("click", () => setSidebar(false));
matchMedia("(max-width: 640px)").addEventListener("change", () => setSidebar(!matchMedia("(max-width: 640px)").matches));

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("theme-toggle").setAttribute("aria-label", "Switch to " + (theme === "dark" ? "light" : "dark") + " theme");
  $("theme-toggle").setAttribute("aria-pressed", String(theme === "dark"));
}
try { applyTheme(localStorage.getItem("agenthub-theme") === "dark" ? "dark" : "light"); }
catch { applyTheme("light"); }
$("theme-toggle").addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(theme);
  try { localStorage.setItem("agenthub-theme", theme); } catch { /* Optional preference storage. */ }
});

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
      const created = await api("/conversations", { title: content.trim().slice(0, 70) });
      conversation = created.id;
      $("chat-title").textContent = created.title;
    }
    // A network retry retains the logical submission key.
    if (!pending || pending.content !== content || pending.conversation !== conversation)
      pending = { content, conversation, submission_key: crypto.randomUUID() };
    const receipt = await api("/conversations/" + conversation + "/messages", {
      content: pending.content, submission_key: pending.submission_key,
    });
    status("Queued", true);
    await renderMessages(true);
    await listConversations();
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
    status("Needs attention");
    document.querySelector(".pending-message")?.remove();
    notice(error.message);
  } finally {
    setBusy(false);
    $("message").focus();
  }
});

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

setSidebar(!matchMedia("(max-width: 640px)").matches);
drawConversations();
resizeComposer();
