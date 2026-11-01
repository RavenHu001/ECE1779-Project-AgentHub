"use strict";
const $ = id => document.getElementById(id);
let authorization = null;
let conversation = null;
let busy = false;
let generation = 0;
let pending = null;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: {Authorization: authorization, "Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (!response.ok) {
    if (response.status === 401) throw new Error("Sign-in failed. Check your username and password.");
    throw new Error(`Request failed (${response.status}). Please try again.`);
  }
  return response.json();
}

function notice(message = "") { $("notice").textContent = message; }
function setBusy(value) {
  busy = value;
  for (const id of ["send", "message", "new-chat", "sign-out"]) $(id).disabled = value;
  document.querySelectorAll("#conversations button").forEach(button => button.disabled = value);
}

async function listConversations() {
  const rows = await api("/conversations");
  $("conversations").replaceChildren();
  for (const row of rows) {
    const button = document.createElement("button");
    button.textContent = row.title;
    button.setAttribute("aria-current", String(row.id === conversation));
    button.disabled = busy;
    button.addEventListener("click", () => openConversation(row).catch(error => notice(error.message)));
    $("conversations").append(button);
  }
  return rows;
}

async function renderMessages() {
  if (!conversation) return;
  // Read all pages so a long saved conversation does not hide its newest replies.
  let rows = [], after = 0;
  while (true) {
    const page = await api(`/conversations/${conversation}/messages?after_seq=${after}&limit=500`);
    rows.push(...page);
    if (page.length < 500) break;
    after = page[page.length - 1].seq;
  }
  $("messages").replaceChildren();
  for (const row of rows) {
    const article = document.createElement("article");
    article.className = `message ${row.role}`;
    const label = document.createElement("strong");
    label.textContent = row.role === "user" ? "You" : "AgentHub";
    const text = document.createElement("p");
    text.textContent = row.content;
    article.append(label, text);
    $("messages").append(article);
  }
  $("messages").scrollTop = $("messages").scrollHeight;
}

async function openConversation(row) {
  if (busy) return;
  pending = null;
  conversation = row.id;
  $("chat-title").textContent = row.title;
  $("job-status").textContent = "Saved history";
  notice();
  await renderMessages();
  await listConversations();
}

$("login-form").addEventListener("submit", async event => {
  event.preventDefault(); notice();
  // HTTP Basic credentials are kept only in memory, never in browser storage.
  authorization = "Basic " + btoa(`${$("username").value}:${$("password").value}`);
  try {
    await api("/me");
    $("password").value = "";
    $("login-panel").hidden = true;
    $("workspace").hidden = false;
    const rows = await listConversations();
    if (rows.length) await openConversation(rows[0]);
  } catch (error) { authorization = null; notice(error.message); }
});

$("new-chat").addEventListener("click", () => {
  pending = null; conversation = null;
  $("chat-title").textContent = "New conversation";
  $("messages").replaceChildren();
  $("job-status").textContent = "Ready";
  $("message").value = ""; $("message").focus(); notice();
  listConversations().catch(error => notice(error.message));
});

$("sign-out").addEventListener("click", () => {
  generation++; authorization = null; conversation = null; pending = null;
  $("workspace").hidden = true; $("login-panel").hidden = false;
  $("messages").replaceChildren(); $("conversations").replaceChildren(); notice();
});

$("message-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (busy) return;
  const content = $("message").value;
  if (!content.trim()) return;
  const currentGeneration = generation;
  setBusy(true); notice();
  try {
    if (!conversation) {
      const created = await api("/conversations", {title: content.trim().slice(0, 70)});
      conversation = created.id; $("chat-title").textContent = created.title;
    }
    // Preserve the same key if a network error leaves submission acceptance uncertain.
    if (!pending || pending.content !== content || pending.conversation !== conversation)
      pending = {content, conversation, submission_key: crypto.randomUUID()};
    const receipt = await api(`/conversations/${conversation}/messages`, {
      content: pending.content, submission_key: pending.submission_key,
    });
    $("job-status").textContent = "Queued";
    await renderMessages();
    const deadline = Date.now() + 180000;
    while (Date.now() < deadline && generation === currentGeneration) {
      const job = await api(`/jobs/${receipt.job_id}`);
      $("job-status").textContent = job.status.replaceAll("_", " ");
      if (job.status === "FAILED") { pending = null; throw new Error(job.last_error || "Worker reported failure."); }
      if (job.status === "SUCCEEDED") {
        pending = null; $("message").value = "";
        await renderMessages(); await listConversations(); return;
      }
      await sleep(750);
    }
    throw new Error("Still waiting for the worker. Resend the same text to check this job without duplicating it.");
  } catch (error) { notice(error.message); }
  finally { setBusy(false); }
});

fetch("/health").then(response => {
  if (!response.ok) throw new Error("API unavailable");
  return response.json();
}).then(health => {
  $("mode").textContent = health.reply_mode === "fixed"
    ? "Demo mode · Fixed replies · No model charges" : "Model chat · API usage billed";
}).catch(() => { $("mode").textContent = "API unavailable"; });
