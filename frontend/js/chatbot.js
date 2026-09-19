// js/chatbot.js
// ---------------------------------------------------------------------------
// Floating assistant drawer, mountable on any citizen page.
//
// Talks to POST /api/v1/chat, which answers strictly from the platform's own
// records. Two consequences the UI has to respect:
//
//   * the response carries `sources` and a `grounded` flag - when the backend
//     had no records it says so rather than answering from training data, and
//     this widget shows that plainly instead of hiding it;
//   * the endpoint needs a token, so a signed-out visitor gets a prompt to
//     sign in rather than a broken send button.
// ---------------------------------------------------------------------------

const CHAT_QUICK_PROMPTS = [
  "Is the Prithvi Highway clear?",
  "Landslide updates in Bagmati",
  "Any flood warnings near me?",
  "What is being fixed in my district?",
];

const CHAT_STORAGE_KEY = "bn_chat_history";
const CHAT_HISTORY_LIMIT = 40;

// Fetched once from GET /health and cached for the page's lifetime. Never
// hardcode a model name here - this is the one place that is allowed to
// claim one, and only after the backend has confirmed it.
let aiStatusPromise = null;
function fetchAiStatus() {
  if (!aiStatusPromise) {
    aiStatusPromise = apiGet("/health", { auth: false })
      .then((data) => data.ai || null)
      .catch(() => null);
  }
  return aiStatusPromise;
}

// A short, honest line from real /health data. Prefers the vision node (used
// by chat()), falls back to text (chat's own fallback node), and says nothing
// specific when the provider is not Ollama or no node answered.
function describeAiStatus(ai) {
  if (!ai || ai.provider !== "ollama" || !ai.nodes) return null;
  const node = ai.nodes.vision?.configured ? ai.nodes.vision : ai.nodes.text;
  if (!node || !node.model) return null;
  // "qwen2.5:7b" -> "Qwen 2.5 7B". Anything that does not fit this shape is
  // shown as the backend sent it rather than guessed at.
  const match = node.model.match(/^([a-z]+)(\d+(?:\.\d+)?):(\d+)b$/i);
  const modelLabel = match
    ? `${match[1][0].toUpperCase()}${match[1].slice(1)} ${match[2]} ${match[3]}B`
    : node.model;
  return { modelLabel, reachable: Boolean(node.reachable) };
}

// Minimal markdown. Deliberately not a library: the only thing an LLM reliably
// emits here is bold, italics, code and lists, and every value is escaped
// first so a model that returns `<script>` renders as text, not as script.
function renderMarkdown(text) {
  const escaped = escapeHtml(text);

  const withInline = escaped
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/`([^`]+)`/g, '<code class="bn-chat-code">$1</code>');

  const lines = withInline.split("\n");
  const out = [];
  let inList = false;

  for (const line of lines) {
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (bullet) {
      if (!inList) {
        out.push('<ul class="bn-chat-list">');
        inList = true;
      }
      out.push(`<li>${bullet[1]}</li>`);
      continue;
    }
    if (inList) {
      out.push("</ul>");
      inList = false;
    }
    if (line.trim()) out.push(`<p>${line}</p>`);
  }
  if (inList) out.push("</ul>");

  return out.join("");
}

function chatHistory() {
  try {
    return JSON.parse(sessionStorage.getItem(CHAT_STORAGE_KEY) || "[]");
  } catch (_) {
    return [];
  }
}

function saveChatHistory(messages) {
  try {
    sessionStorage.setItem(
      CHAT_STORAGE_KEY,
      JSON.stringify(messages.slice(-CHAT_HISTORY_LIMIT))
    );
  } catch (_) {
    /* private browsing: the transcript simply will not persist */
  }
}

// The district the assistant should answer about. Whatever the page is
// currently scoped to, otherwise the signed-in user's current district, then
// their home one - so a question asked from a Kaski feed is answered about
// Kaski without the user having to say so.
function activeDistrictId() {
  const explicit = document.body?.dataset?.districtId;
  if (explicit) return explicit;

  const user = typeof TokenStore !== "undefined" ? TokenStore.getUser() : null;
  return (user && (user.temporary_district_id || user.permanent_district_id)) || null;
}

function mountChatbot() {
  if (document.getElementById("bn-chatbot")) return;

  const root = document.createElement("div");
  root.id = "bn-chatbot";
  root.innerHTML = `
    <style>
      #bn-chatbot { font-family: var(--font-sans); position: fixed; right: 20px; bottom: 20px; z-index: 1000;
        font-family: inherit; }
      .bn-chat-fab { width: 56px; height: 56px; border-radius: 9999px; border: 0;
        background: var(--bn-forest-600); color: #fff; cursor: pointer;
        box-shadow: 0 10px 25px -5px rgba(0,0,0,.3); display: grid;
        place-items: center; transition: transform .15s ease; }
      .bn-chat-fab:hover { transform: scale(1.06); }
      .bn-chat-panel { position: fixed; right: 20px; bottom: 88px; width: min(400px, calc(100vw - 40px));
        height: min(560px, calc(100vh - 140px)); display: flex; flex-direction: column;
        background: rgba(255,255,255,.82); backdrop-filter: blur(16px);
        -webkit-backdrop-filter: blur(16px); border: 1px solid rgba(255,255,255,.6);
        border-radius: 18px; box-shadow: 0 25px 50px -12px rgba(0,0,0,.35);
        overflow: hidden; transform: translateY(12px); opacity: 0;
        pointer-events: none; transition: opacity .18s ease, transform .18s ease; }
      .bn-chat-panel.open { opacity: 1; transform: translateY(0); pointer-events: auto; }
      .bn-chat-head { padding: 14px 16px; background: linear-gradient(135deg,var(--bn-forest-800),var(--bn-forest-600));
        color:#fff; display:flex; align-items:center; justify-content:space-between; gap:10px; }
      .bn-chat-head h3 { margin:0; font-size:15px; font-weight:700; }
      .bn-chat-head p { margin:2px 0 0; font-size:11px; opacity:.85; }
      .bn-chat-status { display:none; align-items:center; gap:5px; margin-top:5px; font-size:10.5px;
        font-weight:600; letter-spacing:.02em; opacity:.9; }
      .bn-chat-status.visible { display:inline-flex; }
      .bn-chat-status-dot { width:6px; height:6px; border-radius:50%; background:#4ade80;
        box-shadow:0 0 0 2px rgba(74,222,128,.25); flex-shrink:0; }
      .bn-chat-status-dot.offline { background:#f59e0b; box-shadow:0 0 0 2px rgba(245,158,11,.25); }
      .bn-chat-close { background:transparent;border:0;color:#fff;font-size:22px;cursor:pointer;line-height:1;
        flex-shrink:0; }
      .bn-chat-log { flex:1; overflow-y:auto; padding:14px; display:flex; flex-direction:column; gap:10px; }
      .bn-chat-row { display:flex; align-items:flex-end; gap:7px; max-width:100%; }
      .bn-chat-row-user { align-self:flex-end; flex-direction:row-reverse; }
      .bn-chat-row-bot { align-self:flex-start; }
      .bn-chat-avatar { flex-shrink:0; width:24px; height:24px; border-radius:50%; display:grid;
        place-items:center; background:var(--bn-forest-700); color:#fff; }
      .bn-chat-avatar svg { width:15px; height:15px; }
      .bn-chat-msg { max-width:min(300px, 78%); padding:9px 12px; border-radius:14px;
        font-size:13px; line-height:1.5; }
      .bn-chat-msg p { margin:0 0 6px; } .bn-chat-msg p:last-child { margin-bottom:0; }
      .bn-chat-list { margin:4px 0 6px; padding-left:18px; }
      .bn-chat-code { background:rgba(0,0,0,.07); padding:1px 4px; border-radius:4px; font-size:12px; }
      .bn-chat-user { background:var(--bn-forest-600); color:#fff; border-bottom-right-radius:4px; }
      .bn-chat-bot { background:#fff; color:var(--bn-charcoal-900);
        border:1px solid var(--bn-border); border-bottom-left-radius:4px; }
      .bn-chat-bot.bn-chat-error { border-color:#f2b8b5; background:#fdf2f2; }
      .bn-chat-note { align-self:flex-start; font-size:11px; color:var(--bn-text-light); padding:0 4px; }
      .bn-chat-typing { display:inline-flex; align-items:center; gap:4px; padding:2px 0; }
      .bn-chat-typing span { width:6px; height:6px; border-radius:50%; background:var(--bn-forest-400);
        animation: bn-chat-bounce 1.1s ease-in-out infinite; }
      .bn-chat-typing span:nth-child(2) { animation-delay:.15s; }
      .bn-chat-typing span:nth-child(3) { animation-delay:.3s; }
      @keyframes bn-chat-bounce { 0%, 60%, 100% { transform:translateY(0); opacity:.5; } 30% { transform:translateY(-4px); opacity:1; } }
      .bn-chat-retry { margin-top:6px; border:1px solid var(--bn-border-strong); background:#fff;
        color:var(--bn-forest-700); border-radius:8px; padding:4px 10px; font-size:11.5px; font-weight:600;
        cursor:pointer; }
      .bn-chat-retry:hover { border-color:var(--bn-forest-600); }
      .bn-chat-prompts { display:flex; flex-wrap:wrap; gap:6px; padding:0 14px 10px; }
      .bn-chat-chip { font-size:11px; padding:5px 10px; border-radius:9999px;
        border:1px solid var(--bn-border-strong); background:#fff; color:var(--bn-charcoal-700); cursor:pointer; }
      .bn-chat-chip:hover { border-color:var(--bn-forest-600); color:var(--bn-forest-700); }
      .bn-chat-form { display:flex; gap:8px; padding:12px; border-top:1px solid var(--bn-border);
        background:rgba(255,255,255,.9); }
      .bn-chat-input { flex:1; border:1px solid var(--bn-border-strong); border-radius:10px; padding:9px 12px;
        font-size:13px; outline:none; font-family:inherit; }
      .bn-chat-input:focus { border-color:var(--bn-forest-600); box-shadow:0 0 0 3px rgba(34, 92, 65, .18); }
      .bn-chat-send { border:0; background:var(--bn-forest-600); color:#fff; border-radius:10px;
        padding:0 16px; font-size:13px; font-weight:600; cursor:pointer; }
      .bn-chat-send:disabled { opacity:.5; cursor:not-allowed; }
      @media (max-width: 480px) {
        .bn-chat-panel { right:10px; left:10px; bottom:80px; width:auto; height:min(72vh, calc(100vh - 120px)); }
        .bn-chat-fab { right:16px; bottom:16px; }
      }
      @media (prefers-reduced-motion: reduce) {
        .bn-chat-fab, .bn-chat-panel, .bn-chat-typing span { transition: none; animation: none; }
      }
    </style>

    <div class="bn-chat-panel" id="bn-chat-panel" role="dialog" aria-label="Better Nepal assistant" aria-modal="false">
      <div class="bn-chat-head">
        <div>
          <h3 data-i18n="chat.title">Better Nepal AI</h3>
          <p data-i18n="chat.subtitle">Answers from reported incidents and official notices</p>
          <span class="bn-chat-status" id="bn-chat-status">
            <span class="bn-chat-status-dot" id="bn-chat-status-dot"></span>
            <span id="bn-chat-status-text"></span>
          </span>
        </div>
        <button class="bn-chat-close" id="bn-chat-close" aria-label="Close assistant">&times;</button>
      </div>
      <div class="bn-chat-log" id="bn-chat-log" aria-live="polite"></div>
      <div class="bn-chat-prompts" id="bn-chat-prompts"></div>
      <form class="bn-chat-form" id="bn-chat-form">
        <input class="bn-chat-input" id="bn-chat-input" placeholder="Ask about your area…"
               autocomplete="off" maxlength="1000" />
        <button class="bn-chat-send" type="submit" id="bn-chat-send">Send</button>
      </form>
    </div>

    <button class="bn-chat-fab" id="bn-chat-fab" aria-label="Open assistant" aria-expanded="false">
      <svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
        <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
      </svg>
    </button>
  `;
  document.body.appendChild(root);

  const panel = document.getElementById("bn-chat-panel");
  const fab = document.getElementById("bn-chat-fab");
  const log = document.getElementById("bn-chat-log");
  const form = document.getElementById("bn-chat-form");
  const input = document.getElementById("bn-chat-input");
  const send = document.getElementById("bn-chat-send");

  // Real technical identity, shown only once the backend has confirmed it -
  // never on the assumption that today's config still matches last deploy's.
  fetchAiStatus().then((ai) => {
    const info = describeAiStatus(ai);
    if (!info) return;
    const statusEl = document.getElementById("bn-chat-status");
    const dot = document.getElementById("bn-chat-status-dot");
    const text = document.getElementById("bn-chat-status-text");
    text.textContent = info.reachable
      ? `${info.modelLabel} · Private AI infrastructure`
      : `${info.modelLabel} · Currently unreachable`;
    dot.classList.toggle("offline", !info.reachable);
    statusEl.classList.add("visible");
  });

  function append(role, html, className = "") {
    const row = document.createElement("div");
    row.className = `bn-chat-row bn-chat-row-${role}`;

    if (role === "bot") {
      const avatar = document.createElement("span");
      avatar.className = "bn-chat-avatar";
      avatar.setAttribute("aria-hidden", "true");
      avatar.innerHTML =
        '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.8">' +
        '<path d="M8 13 L16 4 L24 13 L16 17 Z" stroke-linejoin="round"/>' +
        '<circle cx="16" cy="11" r="1.6" fill="currentColor" stroke="none"/>' +
        '<path d="M3 23 Q 10 16 16 20 T 29 17" stroke-opacity=".55"/></svg>';
      row.appendChild(avatar);
    }

    const bubble = document.createElement("div");
    bubble.className = `bn-chat-msg bn-chat-${role} ${className}`.trim();
    bubble.innerHTML = html;
    row.appendChild(bubble);

    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
    return bubble;
  }

  function note(text) {
    const el = document.createElement("div");
    el.className = "bn-chat-note";
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
  }

  // Replay the transcript so reopening the drawer does not lose the thread.
  const history = chatHistory();
  if (history.length) {
    history.forEach((m) =>
      append(m.role === "user" ? "user" : "bot", m.role === "user" ? escapeHtml(m.text) : renderMarkdown(m.text))
    );
  } else {
    append("bot", renderMarkdown(
      "Hello. Ask me about incidents, road conditions or official notices in your area. " +
      "I answer **only** from what has been reported to the platform."
    ));
  }

  document.getElementById("bn-chat-prompts").innerHTML = CHAT_QUICK_PROMPTS.map(
    (p) => `<button type="button" class="bn-chat-chip">${escapeHtml(p)}</button>`
  ).join("");

  document.querySelectorAll(".bn-chat-chip").forEach((chip) =>
    chip.addEventListener("click", () => {
      input.value = chip.textContent;
      form.requestSubmit();
    })
  );

  function toggle(open) {
    panel.classList.toggle("open", open);
    fab.setAttribute("aria-expanded", String(open));
    if (open) input.focus();
  }

  fab.addEventListener("click", () => toggle(!panel.classList.contains("open")));
  document.getElementById("bn-chat-close").addEventListener("click", () => toggle(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && panel.classList.contains("open")) toggle(false);
  });

  async function sendMessage(message) {
    append("user", escapeHtml(message));
    send.disabled = true;

    const thinking = append(
      "bot",
      '<div class="bn-chat-typing" aria-label="Assistant is typing"><span></span><span></span><span></span></div>'
    );
    const transcript = chatHistory();
    transcript.push({ role: "user", text: message });

    try {
      const districtId = activeDistrictId();
      const data = await apiPost("/chat", {
        message,
        district_id: districtId || undefined,
      });

      thinking.classList.remove("bn-chat-error");
      thinking.innerHTML = renderMarkdown(data.answer || "No answer was returned.");
      transcript.push({ role: "bot", text: data.answer || "" });

      // Say where the answer came from. `grounded: false` means the backend
      // had no matching records and told the model to say so - presenting that
      // as a normal answer would imply knowledge the platform does not have.
      const sourceCount =
        (data.sources?.announcements?.length || 0) + (data.sources?.incidents?.length || 0);
      note(
        data.grounded
          ? `Based on ${sourceCount} record${sourceCount === 1 ? "" : "s"} in your area.`
          : "No matching records were found, so this is not based on platform data."
      );
    } catch (error) {
      const message503 =
        error instanceof ApiError && error.status === 503
          ? "The assistant is not configured on this server yet."
          : reportApiError(error, "The assistant could not answer.");
      thinking.classList.add("bn-chat-error");
      thinking.innerHTML =
        renderMarkdown(message503) +
        '<button type="button" class="bn-chat-retry">Retry</button>';
      thinking.querySelector(".bn-chat-retry")?.addEventListener("click", () => {
        thinking.remove();
        sendMessage(message);
      });
    } finally {
      saveChatHistory(transcript);
      send.disabled = false;
      input.focus();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const message = input.value.trim();
    if (!message) return;

    if (typeof TokenStore === "undefined" || !TokenStore.isSignedIn()) {
      append("bot", renderMarkdown("Please **sign in** to use the assistant."));
      return;
    }

    input.value = "";
    sendMessage(message);
  });
}

// Mount once the page is ready, unless it opted out with data-no-chatbot.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => {
    if (!document.body.hasAttribute("data-no-chatbot")) mountChatbot();
  });
} else if (!document.body.hasAttribute("data-no-chatbot")) {
  mountChatbot();
}
