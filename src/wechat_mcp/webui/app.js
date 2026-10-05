/* 微信自动助手 - 前端逻辑（pywebview JS API） */

let draft = null;       // 当前编辑中的配置
let lastLogSeq = 0;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ---------------- 导航 ----------------
$$("#nav .nav-item").forEach((item) => {
  item.addEventListener("click", () => {
    $$("#nav .nav-item").forEach((n) => n.classList.remove("active"));
    $$(".page").forEach((p) => p.classList.remove("active"));
    item.classList.add("active");
    $("#page-" + item.dataset.page).classList.add("active");
  });
});

// ---------------- 数据绑定 ----------------
// range 滑块的数值标签：由 data-val 指向同页的 <span>，避免写死单个 id。
function rangeLabel(el) {
  const id = el.dataset.val;
  return id ? document.getElementById(id) : null;
}

function fmtRange(value) {
  const n = Number(value);
  return Number.isFinite(n) ? String(Math.round(n * 100) / 100) : "";
}

function bindDraftToControls() {
  $$("[data-key]").forEach((el) => {
    const key = el.dataset.key;
    if (el.type === "checkbox") el.checked = !!draft[key];
    else el.value = draft[key] ?? "";
    if (el.type === "range") {
      const label = rangeLabel(el);
      if (label) label.textContent = fmtRange(draft[key]);
    }

    el.addEventListener("input", () => {
      if (el.type === "checkbox") draft[key] = el.checked;
      else if (el.type === "range") {
        draft[key] = parseFloat(el.value);
        const label = rangeLabel(el);
        if (label) label.textContent = fmtRange(el.value);
      } else draft[key] = el.value;
    });
  });

  // 作用范围 segmented
  const scope = draft.all_groups ? "all" : "custom";
  $$(".seg").forEach((b) => b.classList.toggle("active", b.dataset.scope === scope));
  $("#groups-field").style.display = draft.all_groups ? "none" : "block";
  $("#groups-input").value = (draft.groups || []).join("\n");

  // 人设
  $("#persona-input").value = draft.persona_custom || "";
}

function collectDraft() {
  // segmented / textarea 的值回收到 draft
  draft.all_groups = $$(".seg").find((b) => b.classList.contains("active")).dataset.scope === "all";
  draft.groups = $("#groups-input").value.split("\n").map((s) => s.trim()).filter(Boolean);
  draft.persona_custom = $("#persona-input").value;
  return draft;
}

$$(".seg").forEach((b) => {
  b.addEventListener("click", () => {
    $$(".seg").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("#groups-field").style.display = b.dataset.scope === "custom" ? "block" : "none";
  });
});

// ---------------- 保存 ----------------
async function saveAll() {
  collectDraft();
  const state = await window.pywebview.api.save_config(draft);
  draft = state.config;
  bindDraftToControls();
  renderStatus(state);
  toast("设置已保存");
}
$("#save-chats").addEventListener("click", saveAll);
$("#save-human").addEventListener("click", saveAll);
$("#save-quiet").addEventListener("click", saveAll);
$("#save-links").addEventListener("click", saveAll);
$("#save-model").addEventListener("click", saveAll);
$("#save-persona").addEventListener("click", saveAll);

// ---------------- 总开关 / 连接 ----------------
$("#btn-toggle").addEventListener("click", async () => {
  collectDraft();
  draft.enabled = !draft.enabled;
  const state = await window.pywebview.api.save_config(draft);
  draft = state.config;
  bindDraftToControls();
  renderStatus(state);
  toast(draft.enabled ? "自动回复已开启" : "自动回复已停止");
});

$("#btn-connect").addEventListener("click", async () => {
  $("#btn-connect").disabled = true;
  const result = await window.pywebview.api.connect_wechat();
  $("#btn-connect").disabled = false;
  toast(result.ok ? "微信已连接" : "连接失败：" + (result.error?.message || ""), result.ok ? null : "err");
  refreshState();
});

$("#btn-test").addEventListener("click", async () => {
  collectDraft();
  $("#btn-test").disabled = true;
  const r = await window.pywebview.api.test_connection(draft);
  $("#btn-test").disabled = false;
  toast(r.ok ? "连接正常：" + r.reply.slice(0, 40) : "测试失败：" + r.error.message, r.ok ? null : "err");
});

$("#btn-reset-persona").addEventListener("click", async () => {
  const persona = await window.pywebview.api.reset_persona();
  draft.persona_custom = "";
  $("#persona-input").value = "";
  toast("已清空人设（保存后生效）");
});

// ---------------- 状态渲染 ----------------
function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

function renderStatus(state) {
  const engine = state.engine;
  const w = engine.wechat;

  $("#s-wx").textContent = w.ok ? "已连接" : "未连接";
  $("#s-listen").textContent = w.listening ? "监听中" : "未监听";
  $("#s-bot").textContent = engine.running ? "运行中" : "已停止";
  const btn = $("#btn-toggle");
  btn.textContent = engine.running ? "停止" : "开启";
  btn.classList.toggle("primary", !engine.running);
  btn.style.background = engine.running ? "#fff" : "";
  btn.style.color = engine.running ? "#ef4444" : "";
  btn.style.borderColor = engine.running ? "#fecaca" : "";

  $("#s-count").textContent = engine.reply_count_today;
  $("#s-last").textContent = fmtTime(engine.last_trigger_at);

  // 侧栏底部
  const dot = $("#wx-dot");
  dot.className = "status-dot " + (w.ok ? "ok" : "err");
  $("#wx-title").textContent = w.ok ? "微信已连接" : "未连接微信";
  $("#wx-sub").textContent = w.backend ? "后端：" + w.backend : (w.error?.message || "—");
}

async function refreshState() {
  const state = await window.pywebview.api.get_state();
  // 不覆盖用户正在编辑的草稿，只刷新运行状态
  renderStatus(state);
}

// ---------------- 日志 ----------------
async function pollLogs() {
  const lines = await window.pywebview.api.get_logs(lastLogSeq);
  if (!lines.length) return;
  const box = $("#log-box");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const frag = document.createDocumentFragment();
  lines.forEach((l) => {
    lastLogSeq = l.seq;
    const div = document.createElement("div");
    div.className = "log-line log-" + l.level;
    const t = new Date(l.timestamp * 1000).toLocaleTimeString("zh-CN", { hour12: false });
    div.innerHTML = '<span class="log-time">' + t + "</span>" + escapeHtml(l.message);
    frag.appendChild(div);
  });
  box.appendChild(frag);
  if ($("#log-follow").checked && atBottom) box.scrollTop = box.scrollHeight;
}

$("#btn-clear-log").addEventListener("click", () => { $("#log-box").innerHTML = ""; });

function escapeHtml(s) {
  return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

// ---------------- Toast ----------------
let toastTimer = null;
function toast(msg, kind) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show" + (kind === "err" ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 2600);
}

// ---------------- 启动 ----------------
async function init() {
  const state = await window.pywebview.api.get_state();
  draft = state.config;
  bindDraftToControls();
  renderStatus(state);
  lastLogSeq = 0;
  $("#log-box").innerHTML = "";
  pollLogs();
  setInterval(refreshState, 3000);
  setInterval(pollLogs, 1500);
}

window.addEventListener("pywebviewready", init);
