const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const state = { scopes: [], keywords: [], items: [], page: 1, pageSize: 6, total: 0,
  scope: "", keyword: "", query: "", ready: false, canSaveSettings: false };
const listFields = ["source_bot_qqs", "enabled_groups", "admin_qqs", "ignore_node_patterns", "keyword_aliases"];
const boolFields = ["auto_collect", "answer_queries", "notify_import"];
const numberFields = ["query_timeout", "max_import_quotes"];
let listSequence = 0;
let searchTimer;
let toastTimer;
let editing = null;
let deleting = null;
let settingsDirty = false;
let starting = false;

function message(id, text) {
  $(id).textContent = text;
  $(id).hidden = !text;
}

function errorText(error) {
  return error instanceof Error ? error.message : "操作失败，请稍后重试。";
}

function notify(text) {
  clearTimeout(toastTimer);
  message("toast", text);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3600);
}

function theme(context) {
  if (context && typeof context.isDark === "boolean") {
    document.documentElement.dataset.theme = context.isDark ? "dark" : "light";
  }
}

function fillSelect(element, options, firstLabel, selected = "") {
  element.replaceChildren();
  if (firstLabel !== null) element.add(new Option(firstLabel, ""));
  for (const option of options) element.add(new Option(option.label, option.value));
  element.value = selected;
  if (element.selectedIndex < 0 && element.options.length) element.selectedIndex = 0;
}

function fillSettings(settings) {
  for (const key of listFields) {
    const value = settings[key];
    $("setting-" + key).value = Array.isArray(value) ? value.join("\n") : String(value || "");
  }
  for (const key of boolFields) $("setting-" + key).checked = Boolean(settings[key]);
  for (const key of numberFields) $("setting-" + key).value = settings[key];
  settingsDirty = false;
  $("settings-hint").textContent = state.canSaveSettings ? "保存后立即生效。" : "当前环境无法保存配置，请从 AstrBot 插件配置中设置。";
  $("save-settings").disabled = !state.canSaveSettings;
}

async function loadState() {
  const data = await bridge.apiGet("ui/state");
  state.scopes = data.scopes;
  state.keywords = data.keywords;
  state.canSaveSettings = Boolean(data.can_save_settings);
  for (const key of ["quotes", "keywords", "scopes"]) $("stat-" + key).textContent = data.stats[key].toLocaleString("zh-CN");
  const scopes = state.scopes.map((item) => ({ value: item.scope, label: item.label + " · " + item.detail }));
  fillSelect($("scope-filter"), scopes, "全部会话", state.scope);
  fillSelect($("keyword-filter"), state.keywords.map((item) => ({ value: item.keyword, label: item.keyword })), "全部关键词", state.keyword);
  // Remove filters that no longer exist after an edit/deletion in another tab.
  state.scope = $("scope-filter").value;
  state.keyword = $("keyword-filter").value;
  if (!settingsDirty) fillSettings(data.settings);
}

function displayDate(value) {
  const date = new Date(value.replace(" ", "T") + "Z");
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}

function renderRows() {
  const rows = document.createDocumentFragment();
  for (const quote of state.items) {
    const row = $("quote-row-template").content.cloneNode(true);
    row.querySelector(".quote-text").textContent = quote.body;
    row.querySelector(".quote-meta").textContent = `#${quote.id} · ${displayDate(quote.created_at)}`;
    row.querySelector(".keyword-tag").textContent = quote.keyword;
    row.querySelector(".scope-label").textContent = quote.label;
    row.querySelector(".scope-detail").textContent = quote.detail;
    const edit = row.querySelector(".edit-button");
    const remove = row.querySelector(".delete-button");
    edit.setAttribute("aria-label", `编辑语录 ${quote.id}`);
    remove.setAttribute("aria-label", `删除语录 ${quote.id}`);
    edit.addEventListener("click", () => openEditor(quote));
    remove.addEventListener("click", () => openDelete(quote));
    rows.append(row);
  }
  $("quote-rows").replaceChildren(rows);
  $("empty").hidden = state.items.length > 0;
  const filtered = Boolean(state.query || state.keyword || state.scope);
  $("empty-title").textContent = filtered ? "没有找到匹配的语录" : "还没有收录语录";
  $("empty-description").textContent = filtered ? "换个关键词，或清除筛选再试试。" : "先在 QQ 群中发送“添加 关键词#语录”，此处就会出现对应会话。";
  $("result-count").textContent = `共 ${state.total.toLocaleString("zh-CN")} 条语录`;
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  $("page-number").textContent = `${state.page} / ${pages}`;
  $("previous").disabled = state.page <= 1;
  $("next").disabled = state.page >= pages;
}

async function loadQuotes() {
  const sequence = ++listSequence;
  $("table-region").setAttribute("aria-busy", "true");
  try {
    const data = await bridge.apiGet("ui/quotes", {
      scope: state.scope, keyword: state.keyword, query: state.query,
      page: state.page, page_size: state.pageSize,
    });
    if (sequence !== listSequence) return;
    state.items = data.items;
    state.total = data.total;
    const pages = Math.max(1, Math.ceil(data.total / state.pageSize));
    if (state.page > pages) {
      state.page = pages;
      return await loadQuotes();
    }
    renderRows();
  } finally {
    if (sequence === listSequence) $("table-region").setAttribute("aria-busy", "false");
  }
}

async function refresh() {
  if (!state.ready) return start();
  $("refresh").disabled = true;
  message("error", "");
  try {
    await loadState();
    await loadQuotes();
  } catch (error) {
    message("error", errorText(error));
  } finally {
    $("refresh").disabled = false;
  }
}

async function applyFilters() {
  clearTimeout(searchTimer);
  state.query = $("search").value.trim();
  state.scope = $("scope-filter").value;
  state.keyword = $("keyword-filter").value;
  state.page = 1;
  message("error", "");
  try { await loadQuotes(); } catch (error) { message("error", errorText(error)); }
}

function setTab(name, focus = false) {
  for (const tab of ["quotes", "settings"]) {
    const selected = name === tab;
    $("tab-" + tab).classList.toggle("active", selected);
    $("tab-" + tab).setAttribute("aria-selected", String(selected));
    $("tab-" + tab).tabIndex = selected ? 0 : -1;
    $("panel-" + tab).hidden = !selected;
  }
  if (focus) $("tab-" + name).focus();
}

function openEditor(quote = null) {
  editing = quote;
  message("quote-error", "");
  $("quote-form").reset();
  $("quote-dialog-title").textContent = quote ? "编辑语录" : "添加语录";
  const choices = state.scopes.map((item) => ({ value: item.scope, label: item.label + " · " + item.detail }));
  if (quote && !choices.some((item) => item.value === quote.scope)) {
    choices.push({ value: quote.scope, label: quote.label + " · " + quote.detail });
  }
  const selected = quote?.scope || state.scope || (choices.length === 1 ? choices[0].value : "");
  fillSelect($("quote-scope"), choices, "请选择所属会话", selected);
  $("quote-scope").disabled = Boolean(quote);
  $("quote-keyword").value = quote?.keyword || state.keyword;
  $("quote-body").value = quote?.body || "";
  $("body-count").textContent = `${$("quote-body").value.length} / 30000`;
  $("save-quote").disabled = !choices.length;
  $("scope-help").textContent = !choices.length ? "请先在 QQ 中添加一条语录，刷新后即可选择所属会话。" : quote ? "编辑会保留原来的所属会话。" : "语录只会出现在选中的会话中。";
  $("quote-dialog").showModal();
  if (choices.length) $("quote-keyword").focus();
}

function openDelete(quote) {
  deleting = quote;
  message("delete-error", "");
  $("confirm-delete").disabled = false;
  $("delete-preview").textContent = quote.body;
  $("delete-keyword").textContent = `关键词：${quote.keyword} · ${quote.label}`;
  $("delete-dialog").showModal();
  $("delete-dialog").querySelector("[data-close]").focus();
}

$("quote-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  message("quote-error", "");
  const payload = { scope: $("quote-scope").value, keyword: $("quote-keyword").value, body: $("quote-body").value };
  if (editing) Object.assign(payload, { id: editing.id, version: editing.version });
  const updating = Boolean(editing);
  $("save-quote").disabled = true;
  try {
    await bridge.apiPost("ui/save", payload);
    $("quote-dialog").close();
    notify(updating ? "语录已更新。" : "语录已添加。" );
    await refresh();
  } catch (error) {
    message("quote-error", errorText(error));
  } finally {
    $("save-quote").disabled = false;
  }
});

$("delete-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!deleting) return;
  message("delete-error", "");
  $("confirm-delete").disabled = true;
  try {
    await bridge.apiPost("ui/delete", { id: deleting.id, version: deleting.version });
    $("delete-dialog").close();
    notify("语录已删除。" );
    await refresh();
  } catch (error) {
    message("delete-error", errorText(error));
  } finally {
    $("confirm-delete").disabled = false;
  }
});

$("settings-form").addEventListener("input", () => {
  settingsDirty = true;
  $("settings-hint").textContent = "有未保存的修改。";
});
$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  message("settings-error", "");
  if (!state.canSaveSettings) return;
  const payload = {};
  for (const key of listFields) {
    const value = $("setting-" + key).value;
    payload[key] = value.split(["ignore_node_patterns", "keyword_aliases"].includes(key) ? /\r?\n/ : /[,，\s]+/).map((item) => item.trim()).filter(Boolean);
  }
  for (const key of boolFields) payload[key] = $("setting-" + key).checked;
  for (const key of numberFields) payload[key] = Number($("setting-" + key).value);
  $("save-settings").disabled = true;
  try {
    const result = await bridge.apiPost("ui/settings", payload);
    fillSettings(result.settings);
    await loadState();
    await loadQuotes();
    const merged = result.alias_merge;
    notify(merged && (merged.moved || merged.duplicates)
      ? `设置已保存。已归并 ${merged.moved} 条语录，去重 ${merged.duplicates} 条。`
      : "设置已保存，并立即生效。");
  } catch (error) {
    message("settings-error", errorText(error));
  } finally {
    $("save-settings").disabled = !state.canSaveSettings;
  }
});

$("refresh").addEventListener("click", refresh);
$("add").addEventListener("click", () => openEditor());
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  listSequence++; // An older in-flight request must not replace the new search.
  if (state.ready) searchTimer = setTimeout(applyFilters, 250);
});
$("filters").addEventListener("submit", (event) => { event.preventDefault(); if (state.ready) applyFilters(); });
for (const id of ["scope-filter", "keyword-filter"]) $(id).addEventListener("change", () => { if (state.ready) applyFilters(); });
$("clear-filters").addEventListener("click", () => {
  $("search").value = $("scope-filter").value = $("keyword-filter").value = "";
  if (state.ready) applyFilters();
});
for (const [id, delta] of [["previous", -1], ["next", 1]]) $(id).addEventListener("click", async () => {
  state.page += delta;
  try { await loadQuotes(); } catch (error) { message("error", errorText(error)); }
});
for (const name of ["quotes", "settings"]) {
  $("tab-" + name).addEventListener("click", () => setTab(name));
  $("tab-" + name).addEventListener("keydown", (event) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      setTab(event.key === "Home" ? "quotes" : event.key === "End" ? "settings" : name === "quotes" ? "settings" : "quotes", true);
    }
  });
}
for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => $(button.dataset.close).close());
$("quote-body").addEventListener("input", () => { $("body-count").textContent = `${$("quote-body").value.length} / 30000`; });

async function start() {
  if (starting) return;
  starting = true;
  $("refresh").disabled = true;
  try {
    if (!bridge) throw new Error("请从 AstrBot 插件详情中的“语录库”打开此页面。需要支持插件可视化视图的 AstrBot 版本。" );
    let readyTimer;
    try {
      const context = await Promise.race([
        bridge.ready(),
        new Promise((_, reject) => { readyTimer = setTimeout(() => reject(new Error("连接 AstrBot 超时，请刷新插件视图。")), 12000); }),
      ]);
      theme(context);
    } finally { clearTimeout(readyTimer); }
    const off = bridge.onContext(theme);
    window.addEventListener("beforeunload", () => { off(); clearTimeout(searchTimer); clearTimeout(toastTimer); });
    await loadState();
    state.ready = true;
    $("add").disabled = false;
    await loadQuotes();
  } catch (error) {
    message("error", errorText(error));
    $("empty-title").textContent = "暂时无法连接语录库";
    $("empty-description").textContent = "请确认插件已启用，并使用 AstrBot WebUI 管理员账号打开。";
    $("result-count").textContent = "加载失败";
    $("table-region").setAttribute("aria-busy", "false");
  } finally {
    starting = false;
    $("refresh").disabled = false;
  }
}
start();
