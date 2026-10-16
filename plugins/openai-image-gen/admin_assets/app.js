"use strict";

const $ = (id) => document.getElementById(id);
const PAGE_SIZES = [12, 24, 48, 96];
const store = {
  get(key, fallback) {
    try {
      return localStorage.getItem("shiguang." + key) ?? fallback;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem("shiguang." + key, value);
    } catch {
      /* private mode etc. — preference just isn't remembered */
    }
  },
};

const state = {
  csrf: "",
  page: 1,
  pages: 1,
  total: 0,
  perPage: PAGE_SIZES.includes(Number(store.get("perPage"))) ? Number(store.get("perPage")) : 24,
  sort: ["newest", "oldest", "largest"].includes(store.get("sort")) ? store.get("sort") : "newest",
  items: [],
  selected: new Set(),
  selectMode: false,
  previewIndex: -1,
  detour: [], // reference images opened from the preview, newest last

  pendingDelete: [],
  loading: false,
};

/* ---------- helpers ---------- */
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
const ICONS = {
  link: ["M8.5 11.5l3-3", "M10.5 5.5l1-1a3.2 3.2 0 0 1 4.5 4.5l-1 1", "M9.5 14.5l-1 1a3.2 3.2 0 0 1-4.5-4.5l1-1"],
  download: ["M10 3.5v9", "M6 9l4 4 4-4", "M4 16.5h12"],
  check: ["M4.5 10.5l3.5 3.5 7.5-8"],
};
function icon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("aria-hidden", "true");
  for (const d of ICONS[name]) {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}
function formatBytes(n) {
  if (n >= 1073741824) {
    const gb = n / 1073741824;
    return (Number.isInteger(gb) ? gb : gb.toFixed(2)) + " GB";
  }
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  return Math.max(1, Math.round(n / 1024)) + " KB";
}
function formatTime(seconds) {
  const d = new Date(seconds * 1000);
  const pad = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
const thumbUrl = (name) => "/admin/api/thumbnail/" + encodeURIComponent(name);
const imageUrl = (name) => "/admin/api/image/" + encodeURIComponent(name);

let toastTimer;
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 3500);
}

async function api(path, data) {
  const options = { credentials: "same-origin", headers: {} };
  if (data !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-CSRF-Token": state.csrf };
    options.body = JSON.stringify(data);
  }
  const response = await fetch("/admin/api/" + path, options);
  let result = {};
  try {
    result = await response.json();
  } catch {
    /* non-JSON error page */
  }
  if (!response.ok) {
    if (response.status === 401 && path !== "login") showLogin();
    const error = new Error(result.error || "请求失败，请稍后再试。");
    error.status = response.status;
    throw error;
  }
  return result;
}

/* ---------- views ---------- */
function showLogin() {
  state.csrf = "";
  state.selectMode = false;
  state.selected.clear();
  for (const d of document.querySelectorAll("dialog[open]")) d.close();
  $("library-view").hidden = true;
  $("login-view").hidden = false;
  $("loading").hidden = true;
  $("gallery").replaceChildren();
  $("password").focus();
}
function showLibrary() {
  $("login-view").hidden = true;
  $("library-view").hidden = false;
  $("loading").hidden = true;
}

/* ---------- loading ---------- */
async function load({ keepSelection = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  $("refresh").disabled = true;
  try {
    const params = new URLSearchParams({
      page: String(state.page),
      per_page: String(state.perPage),
      q: $("search").value.trim(),
      sort: state.sort,
    });
    const data = await api("state?" + params);
    state.csrf = data.csrf;
    state.items = data.items;
    state.page = data.page;
    state.pages = data.pages;
    state.total = data.total;
    state.perPage = data.per_page || state.perPage;
    if (!keepSelection) state.selected.clear();
    showLibrary();
    renderMeta(data);
    renderGallery();
  } catch (e) {
    if (e.status !== 401) {
      $("loading").hidden = true;
      toast(e.message);
    }
  } finally {
    state.loading = false;
    $("refresh").disabled = false;
  }
}

function renderMeta(data) {
  const searching = $("search").value.trim() !== "";
  $("count").textContent = searching ? `找到 ${data.total} / ${data.count} 张` : `${data.count} 张`;
  const pct = Math.min(100, (data.used / data.quota) * 100);
  $("storage-fill").style.width = pct + "%";
  $("storage-text").textContent = `${formatBytes(data.used)} / ${formatBytes(data.quota)} · 剩余 ${formatBytes(data.free)}`;
  $("storage").title = `低于 ${formatBytes(data.reserve)} 可用空间时暂停生图`;
  $("storage").classList.toggle("warn", data.paused);
  $("storage-mobile").textContent = `已用 ${formatBytes(data.used)} / ${formatBytes(data.quota)}`;
  $("warning").hidden = !data.paused;
  $("busy").hidden = !data.busy;

  renderPages();
  $("page-label").textContent = `第 ${state.page} / ${state.pages} 页`;
  $("previous").disabled = state.page <= 1;
  $("next").disabled = state.page >= state.pages;
  for (const b of $("per-page-group").children) {
    b.setAttribute("aria-pressed", String(Number(b.dataset.size) === state.perPage));
  }
  for (const b of $("sort-group").children) {
    b.setAttribute("aria-pressed", String(b.dataset.sort === state.sort));
  }
  $("sort-mobile").value = state.sort;

  $("empty").hidden = state.items.length > 0;
  $("empty-title").textContent = searching ? "没有找到匹配的图片" : "还没有图片";
  $("empty-description").textContent = searching
    ? "换个关键词试试，或者清空搜索查看全部图片。"
    : "在 Claude 里生成图片后，点刷新就能在这里看到。";
}

/* ---------- pages ---------- */
function pageList(current, total) {
  // All pages when few; otherwise first, last, and a window around the current page.
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const keep = new Set([1, total, current - 1, current, current + 1]);
  if (current <= 3) [2, 3, 4].forEach((n) => keep.add(n));
  if (current >= total - 2) [total - 3, total - 2, total - 1].forEach((n) => keep.add(n));
  const pages = [...keep].filter((n) => n >= 1 && n <= total).sort((a, b) => a - b);
  const out = [];
  pages.forEach((n, i) => {
    if (i && n - pages[i - 1] > 1) out.push(null);
    out.push(n);
  });
  return out;
}
function renderPages() {
  const fragment = document.createDocumentFragment();
  for (const n of pageList(state.page, state.pages)) {
    if (n === null) {
      fragment.append(el("span", "page-gap", "…"));
      continue;
    }
    const b = el("button", "page-num", String(n));
    b.setAttribute("aria-label", `第 ${n} 页`);
    if (n === state.page) b.setAttribute("aria-current", "page");
    else b.addEventListener("click", () => goToPage(n));
    fragment.append(b);
  }
  $("page-numbers").replaceChildren(fragment);
}
function goToPage(n) {
  if (state.loading || n < 1 || n > state.pages || n === state.page) return;
  state.page = n;
  load().then(() => window.scrollTo({ top: 0 }));
}

/* ---------- gallery ---------- */
function renderGallery() {
  const fragment = document.createDocumentFragment();
  state.items.forEach((item, index) => {
    const tile = el("figure", item.alpha ? "tile has-alpha" : "tile");
    tile.dataset.name = item.name;

    const open = el("button", "tile-open");
    open.setAttribute("aria-label", "预览 " + item.name);
    const img = el("img");
    img.src = thumbUrl(item.name);
    img.alt = item.name;
    img.loading = "lazy";
    img.decoding = "async";
    open.append(img);
    open.addEventListener("click", () => {
      if (state.selectMode || state.selected.size) toggle(item.name);
      else openPreview(index);
    });

    const check = el("label", "tile-check");
    const input = el("input");
    input.type = "checkbox";
    input.setAttribute("aria-label", "选择 " + item.name);
    input.addEventListener("change", () => toggle(item.name, input.checked));
    const badge = el("span", "badge");
    badge.append(icon("check"));
    check.append(input, badge);

    const tools = el("div", "tile-tools");
    const copyBtn = el("button", "tool");
    copyBtn.setAttribute("aria-label", "复制分享链接");
    copyBtn.title = "复制分享链接";
    copyBtn.append(icon("link"));
    copyBtn.addEventListener("click", () => copy(item.url));
    const dl = el("a", "tool");
    dl.href = item.url;
    dl.download = item.name;
    dl.setAttribute("aria-label", "下载原图");
    dl.title = "下载原图";
    dl.append(icon("download"));
    tools.append(copyBtn, dl);

    const caption = el("figcaption", "tile-caption");
    caption.append(el("span", "", item.name), el("span", "", formatBytes(item.size)));

    tile.append(open, check, tools, caption);
    fragment.append(tile);
  });
  $("gallery").replaceChildren(fragment);
  renderSelection();
}

function toggle(name, on) {
  const yes = on === undefined ? !state.selected.has(name) : on;
  if (yes) state.selected.add(name);
  else state.selected.delete(name);
  renderSelection();
}
function exitSelection() {
  state.selectMode = false;
  state.selected.clear();
  renderSelection();
}
function renderSelection() {
  const n = state.selected.size;
  const active = state.selectMode || n > 0;
  $("gallery").classList.toggle("selecting", active);
  $("selection-bar").hidden = !active;
  $("select-mode").hidden = active;
  $("selection-count").textContent = n;
  $("delete-selected").disabled = n === 0;
  $("download-selected").disabled = n === 0;
  const all = state.items.length > 0 && n === state.items.length;
  $("select-all").textContent = all ? "全不选" : "全选";
  for (const tile of $("gallery").children) {
    const yes = state.selected.has(tile.dataset.name);
    tile.classList.toggle("selected", yes);
    tile.querySelector("input").checked = yes;
  }
}

async function copy(url) {
  try {
    await navigator.clipboard.writeText(url);
    toast("分享链接已复制，只能打开这一张图片。");
  } catch {
    toast("复制没有成功，可以在大图预览里下载原图后再分享。");
  }
}

function downloadSelected() {
  const items = state.items.filter((x) => state.selected.has(x.name));
  items.forEach((item, i) => {
    setTimeout(() => {
      const a = el("a");
      a.href = item.url;
      a.download = item.name;
      document.body.append(a);
      a.click();
      a.remove();
    }, i * 400);
  });
  if (items.length > 1) toast(`正在下载 ${items.length} 张图片，浏览器可能会询问是否允许多个下载。`);
}

/* ---------- preview ---------- */
function currentItem() {
  return state.detour.length ? state.detour[state.detour.length - 1] : state.items[state.previewIndex];
}
function findItem(name) {
  return state.items.find((x) => x.name === name) || state.detour.find((x) => x.name === name);
}
function openPreview(index) {
  state.previewIndex = index;
  state.detour = [];
  renderPreview();
  if (!$("preview-dialog").open) $("preview-dialog").showModal();
}
async function openReference(name) {
  try {
    const data = await api("item/" + encodeURIComponent(name));
    state.detour.push(data.item);
    renderPreview();
  } catch (e) {
    toast(e.message);
  }
}
function previewBack() {
  state.detour.pop();
  renderPreview();
}

const KIND = { generate: "文字生成", edit: "编辑（基于参考图）" };
function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}
function refTile(ref, label) {
  if (ref.name && ref.exists) {
    const b = el("button", "ref");
    b.title = "查看 " + ref.name;
    const img = el("img");
    img.src = thumbUrl(ref.name);
    img.alt = ref.name;
    img.loading = "lazy";
    b.append(img, el("span", "ref-name", label ? `${label} · ${ref.name}` : ref.name));
    b.addEventListener("click", () => openReference(ref.name));
    return b;
  }
  const box = el("div", "ref ref-missing");
  const ph = el("span", "ref-ph", ref.name ? "已删除" : "未保存");
  const text = ref.name || (ref.url ? hostOf(ref.url) : "外部图片");
  box.append(ph, el("span", "ref-name", label ? `${label} · ${text}` : text));
  box.title = ref.name ? `参考图 ${ref.name} 已删除，提示词照样保留` : "这张参考图当时没能保存副本";
  return box;
}
function renderRecord(item) {
  const r = item.record;
  const generated = r && r.kind !== "reference";
  $("preview-record").hidden = !generated;
  $("preview-norecord").hidden = !!r;
  $("preview-source").hidden = !(r && r.kind === "reference");
  if (r && r.kind === "reference") {
    $("preview-source").textContent = "这是编辑时自动保存的参考图副本" + (r.url ? `，原图来自 ${hostOf(r.url)}。` : "。");
  }
  const users = item.used_by_count || 0;
  $("preview-used-by").hidden = users === 0;
  $("preview-used-by").textContent = `这张图是 ${users} 张图的参考图。`;
  if (!generated) return;

  $("record-title").textContent = "提示词";
  $("preview-prompt").textContent = r.prompt || "（空）";
  $("revised-wrap").hidden = !r.revised_prompt;
  $("revised-wrap").open = false;
  $("preview-revised").textContent = r.revised_prompt || "";

  const params = document.createDocumentFragment();
  const rows = [
    ["方式", KIND[r.kind] || r.kind || "—"],
    ["模型", r.model],
    ["画质", r.quality],
    ["请求尺寸", r.size],
    ["背景", r.background],
  ];
  for (const [k, v] of rows) {
    if (!v) continue;
    params.append(el("dt", "", k), el("dd", "", String(v)));
  }
  $("record-params").replaceChildren(params);

  const refs = document.createDocumentFragment();
  (r.references || []).forEach((ref) => refs.append(refTile(ref)));
  if (r.mask) refs.append(refTile(r.mask, "遮罩"));
  $("preview-refs").replaceChildren(refs);
  $("refs-wrap").hidden = !(r.references || []).length && !r.mask;
}

function renderPreview() {
  const item = currentItem();
  if (!item) return;
  const inDetour = state.detour.length > 0;
  $("preview-back").hidden = !inDetour;
  $("preview-image").src = imageUrl(item.name);
  $("preview-image").alt = item.name;
  $("preview-name").textContent = item.name;
  $("preview-dims").textContent = item.width ? `${item.width} × ${item.height}` : "—";
  $("preview-size").textContent = formatBytes(item.size);
  $("preview-format").textContent = item.format || "—";
  $("preview-time").textContent = formatTime(item.modified);
  $("preview-download").href = item.url;
  $("preview-download").download = item.name;
  renderRecord(item);
  $("preview-info").scrollTop = 0;
  const absolute = (state.page - 1) * state.perPage + state.previewIndex + 1;
  $("preview-position").textContent = inDetour ? "正在查看参考图" : `第 ${absolute} / ${state.total} 张`;
  $("preview-prev").disabled = inDetour || absolute <= 1;
  $("preview-next").disabled = inDetour || absolute >= state.total;
}
async function step(delta) {
  if (state.detour.length) return;
  const next = state.previewIndex + delta;
  if (next >= 0 && next < state.items.length) {
    openPreview(next);
    return;
  }
  const target = state.page + delta;
  if (target < 1 || target > state.pages) return;
  state.page = target;
  await load();
  openPreview(delta > 0 ? 0 : state.items.length - 1);
}

/* ---------- delete ---------- */
function askDelete(names) {
  state.pendingDelete = names;
  const n = names.length;
  $("delete-title").textContent = `删除 ${n} 张图片？`;
  $("confirm-delete").textContent = n === 1 ? "删除" : `删除 ${n} 张`;
  const thumbs = document.createDocumentFragment();
  for (const name of names.slice(0, 5)) {
    const img = el("img");
    img.src = thumbUrl(name);
    img.alt = name;
    thumbs.append(img);
  }
  if (n > 5) thumbs.append(el("span", "more", "+" + (n - 5)));
  $("delete-thumbs").replaceChildren(thumbs);

  // Warn when other images were made from these (their records keep the prompt).
  const deleting = new Set(names);
  const users = new Set();
  let referenced = 0;
  let extra = 0;
  for (const name of names) {
    const item = findItem(name);
    if (!item || !item.used_by_count) continue;
    referenced += 1;
    item.used_by.filter((u) => !deleting.has(u)).forEach((u) => users.add(u));
    extra += Math.max(0, item.used_by_count - item.used_by.length);
  }
  const k = users.size + extra;
  $("delete-refs").hidden = k === 0;
  if (k) {
    const lead = n === 1 ? `这张图是 ${k} 张图的参考图。` : `其中 ${referenced} 张是别的图的参考图，共有 ${k} 张图用到。`;
    $("delete-refs").textContent = lead + "删除后，那些图的记录里会显示“参考图已删除”，提示词照样保留。";
  }
  $("delete-dialog").showModal();
}
async function confirmDelete() {
  $("confirm-delete").disabled = true;
  try {
    const result = await api("delete", { names: state.pendingDelete });
    toast(`已删除 ${result.deleted.length} 张图片。`);
  } catch (e) {
    toast(e.message);
  } finally {
    $("confirm-delete").disabled = false;
    $("delete-dialog").close();
    if ($("preview-dialog").open) $("preview-dialog").close();
    state.selectMode = false;
    state.pendingDelete = [];
    await load();
  }
}

/* ---------- events ---------- */
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-button").disabled = true;
  $("login-error").textContent = "";
  try {
    store.set("remember", $("remember").checked ? "1" : "0");
    const data = await api("login", { password: $("password").value, remember: $("remember").checked });
    state.csrf = data.csrf;
    $("password").value = "";
    await load();
  } catch (e) {
    $("login-error").textContent = e.message;
  } finally {
    $("login-button").disabled = false;
  }
});
$("remember").checked = store.get("remember", "1") !== "0";
$("show-password").addEventListener("click", () => {
  const show = $("password").type === "password";
  $("password").type = show ? "text" : "password";
  $("show-password").setAttribute("aria-pressed", String(show));
  $("show-password").setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
});

async function logout() {
  try {
    await api("logout", {});
  } catch {
    /* already logged out */
  }
  showLogin();
}
$("logout").addEventListener("click", logout);
$("logout-mobile").addEventListener("click", logout);
$("refresh").addEventListener("click", () => load());

let searchTimer;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.page = 1;
    load();
  }, 300);
});
$("search-toggle").addEventListener("click", () => {
  const topbar = document.querySelector(".topbar");
  const on = !topbar.classList.contains("searching");
  topbar.classList.toggle("searching", on);
  $("search-toggle").setAttribute("aria-expanded", String(on));
  if (on) $("search").focus();
});
$("search").addEventListener("blur", () => {
  if (!$("search").value) document.querySelector(".topbar").classList.remove("searching");
});

function setSort(sort) {
  state.sort = sort;
  store.set("sort", sort);
  state.page = 1;
  load();
}
for (const b of $("sort-group").children) b.addEventListener("click", () => setSort(b.dataset.sort));
$("sort-mobile").addEventListener("change", () => setSort($("sort-mobile").value));

for (const b of $("per-page-group").children) {
  b.addEventListener("click", () => {
    const first = (state.page - 1) * state.perPage;
    state.perPage = Number(b.dataset.size);
    store.set("perPage", state.perPage);
    state.page = Math.floor(first / state.perPage) + 1;
    load();
  });
}
$("previous").addEventListener("click", () => goToPage(state.page - 1));
$("next").addEventListener("click", () => goToPage(state.page + 1));

$("select-mode").addEventListener("click", () => {
  state.selectMode = true;
  renderSelection();
});
$("cancel-select").addEventListener("click", exitSelection);
$("select-all").addEventListener("click", () => {
  const all = state.items.length > 0 && state.selected.size === state.items.length;
  state.selected = all ? new Set() : new Set(state.items.map((x) => x.name));
  renderSelection();
});
$("download-selected").addEventListener("click", downloadSelected);
$("delete-selected").addEventListener("click", () => askDelete([...state.selected]));

$("preview-prev").addEventListener("click", () => step(-1));
$("preview-next").addEventListener("click", () => step(1));
$("close-preview").addEventListener("click", () => $("preview-dialog").close());
$("preview-copy").addEventListener("click", () => {
  const item = currentItem();
  if (item) copy(item.url);
});
$("preview-delete").addEventListener("click", () => {
  const item = currentItem();
  if (item) askDelete([item.name]);
});
$("preview-back").addEventListener("click", previewBack);
$("copy-prompt").addEventListener("click", async () => {
  const item = currentItem();
  const prompt = item && item.record && item.record.prompt;
  if (!prompt) return;
  try {
    await navigator.clipboard.writeText(prompt);
    toast("提示词已复制。");
  } catch {
    toast("复制没有成功，可以直接选中文字复制。");
  }
});
$("preview-dialog").addEventListener("close", () => {
  $("preview-image").removeAttribute("src");
  state.previewIndex = -1;
  state.detour = [];
});
$("preview-dialog").addEventListener("click", (event) => {
  if (event.target === $("preview-dialog")) $("preview-dialog").close();
});
$("cancel-delete").addEventListener("click", () => $("delete-dialog").close());
$("confirm-delete").addEventListener("click", confirmDelete);

document.addEventListener("keydown", (event) => {
  if ($("delete-dialog").open) return;
  if ($("preview-dialog").open) {
    if (event.key === "Backspace" && state.detour.length) previewBack();
    if (event.key === "ArrowLeft") step(-1);
    if (event.key === "ArrowRight") step(1);
    return;
  }
  if (event.key === "Escape" && (state.selectMode || state.selected.size)) exitSelection();
});

load();
