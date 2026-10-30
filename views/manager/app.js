const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const state = { scopes: [], keywords: [], items: [], page: 1, pageSize: 6, total: 0,
  scope: "", libraries: [], query: "", ready: false, canSaveSettings: false,
  librariesExpanded: false,
  selection: new Map(), loading: false, listReady: false, limits: {filter_libraries: 100, selection: 500} };
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
let batchDeleting = [];
let batchBusy = false;

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
  $("runtime-build").textContent = data.runtime?.build
    ? `已加载 ${data.runtime.version} · 构建 ${data.runtime.build}`
    : "后端构建未确认，请重载插件或重启 AstrBot 后刷新。";
  $("runtime-build").title = "来自当前 AstrBot 进程实际加载的插件；同为 1.0 时，可用构建标识确认更新是否生效。";
  state.limits = data.limits || state.limits;
  for (const key of ["quotes", "keywords", "scopes"]) $("stat-" + key).textContent = data.stats[key].toLocaleString("zh-CN");
  const scopes = state.scopes.map((item) => ({ value: item.scope, label: item.label + " · " + item.detail }));
  fillSelect($("scope-filter"), scopes, "全部会话", state.scope);
  // Remove filters that no longer exist after an edit/deletion in another tab.
  state.scope = $("scope-filter").value;
  state.libraries = state.libraries.filter(name => state.keywords.some(item => item.keyword === name));
  renderLibraries();
  if (!settingsDirty) fillSettings(data.settings);
}

function renderLibraries() {
  const search = $("library-search").value.trim().toLocaleLowerCase();
  const options = document.createDocumentFragment();
  const matches = state.keywords.filter(library => [library.keyword, ...(library.aliases || [])]
    .some(name => name.toLocaleLowerCase().includes(search)));
  if (!search) matches.sort((a, b) => Number(state.libraries.includes(b.keyword)) - Number(state.libraries.includes(a.keyword)));
  const visible = search || state.librariesExpanded ? matches : matches.slice(0, 4);
  for (const library of visible) {
    const card = document.createElement("article");
    card.className = `library-card tone-${libraryTone(library.keyword)}`;
    card.dataset.keyword = library.keyword;
    const open = document.createElement("button");
    open.type = "button";
    open.className = "library-open";
    open.setAttribute("aria-label", `查看主库 ${library.keyword}`);
    open.addEventListener("click", () => {
      state.libraries = state.libraries.length === 1 && state.libraries[0] === library.keyword ? [] : [library.keyword];
      updateLibraryLabel();
      if (state.ready) applyFilters();
    });
    const avatar = document.createElement("span");
    avatar.className = "library-avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.textContent = [...library.keyword][0];
    const name = document.createElement("strong");
    name.className = "library-name";
    name.textContent = library.keyword;
    name.title = library.keyword;
    const counts = document.createElement("span");
    counts.className = "library-counts";
    const number = document.createElement("strong");
    number.textContent = library.count.toLocaleString("zh-CN");
    const unit = document.createElement("span");
    unit.textContent = "条语录";
    const scopes = document.createElement("small");
    scopes.textContent = typeof library.scope_count === "number" ? `${library.scope_count} 个会话` : "所有会话合计";
    counts.append(number, unit, scopes);
    const aliases = document.createElement("span");
    aliases.className = "library-aliases";
    aliases.textContent = library.aliases?.length ? `别名：${library.aliases.join("、")}` : "暂无别名";
    aliases.title = aliases.textContent;
    open.append(avatar, name, counts, aliases);
    const input = document.createElement("input");
    input.type = "checkbox";
    input.className = "library-check";
    input.setAttribute("aria-label", `筛选主库 ${library.keyword}`);
    input.addEventListener("change", () => {
      const selected = new Set(state.libraries);
      if (input.checked) selected.add(library.keyword); else selected.delete(library.keyword);
      if (selected.size > state.limits.filter_libraries) {
        input.checked = false;
        return notify(`一次最多筛选 ${state.limits.filter_libraries} 个语录库。`);
      }
      state.libraries = [...selected];
      updateLibraryLabel();
      if (state.ready) applyFilters();
    });
    card.append(open, input);
    options.append(card);
  }
  $("library-options").replaceChildren(options);
  $("library-empty").hidden = matches.length > 0;
  $("library-empty").textContent = state.keywords.length ? "没有匹配的主库，试试其他库名或别名。"
    : "还没有语录主库。先在 QQ 中添加语录，或引用原记录回复“复刻”。";
  $("library-total").textContent = state.keywords.length.toLocaleString("zh-CN");
  $("library-show-more").hidden = Boolean(search) || matches.length <= 4;
  $("library-show-more").textContent = state.librariesExpanded ? "收起主库" : `展开其余 ${matches.length - visible.length} 个主库`;
  updateLibraryLabel();
}

function libraryTone(name) {
  let hash = 0;
  for (const char of name) hash = (hash * 31 + char.codePointAt(0)) % 5;
  return hash;
}

function updateLibraryLabel() {
  $("library-filter-label").textContent = state.libraries.length ? `已选择 ${state.libraries.length} 个主库` : "未限定主库";
  for (const card of $("library-options").children) {
    const selected = state.libraries.includes(card.dataset.keyword);
    card.classList.toggle("is-active", selected);
    card.querySelector("input").checked = selected;
    card.querySelector("button").setAttribute("aria-pressed", String(selected));
  }
  updateLibraryContext();
}

function updateLibraryContext() {
  const single = state.libraries.length === 1;
  $("active-library-title").textContent = single ? state.libraries[0]
    : state.libraries.length ? `已选择 ${state.libraries.length} 个主库` : "全部主库的语录";
  const library = single ? state.keywords.find(item => item.keyword === state.libraries[0]) : null;
  const names = state.libraries.slice(0, 3).join("、") + (state.libraries.length > 3 ? "等" : "");
  const detail = single ? (library?.aliases?.length ? `别名：${library.aliases.join("、")}` : "按主词条归档的语录")
    : state.libraries.length ? names : "点击上方主库卡片，集中浏览对应语录。";
  $("active-library-detail").textContent = detail;
  $("active-library-detail").title = detail;
  $("active-library-count").textContent = state.loading || !state.listReady ? "加载中…" : `${state.total.toLocaleString("zh-CN")} 条匹配`;
}

function updateSelection() {
  const count = state.selection.size;
  const onPage = state.items.filter(quote => state.selection.has(quote.id)).length;
  $("selection-count").textContent = `已选 ${count} 条${count ? "（可跨页）" : ""}`;
  $("select-page").checked = state.items.length > 0 && onPage === state.items.length;
  $("select-page").indeterminate = onPage > 0 && onPage < state.items.length;
  const unavailable = state.loading || !state.listReady;
  $("select-page").disabled = unavailable || !state.items.length;
  $("clear-selection").disabled = !count;
  $("batch-delete").disabled = !count || unavailable;
  for (const input of $("quote-rows").querySelectorAll(".row-select")) {
    input.checked = state.selection.has(Number(input.dataset.id));
    input.disabled = unavailable;
    input.closest("tr").classList.toggle("is-selected", input.checked);
  }
  for (const button of $("quote-rows").querySelectorAll("button")) button.disabled = unavailable;
  updateLibraryContext();
}

function clearSelection() {
  state.selection.clear();
  updateSelection();
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
    const selected = row.querySelector(".row-select");
    selected.dataset.id = quote.id;
    selected.setAttribute("aria-label", `选择语录 ${quote.id}`);
    selected.addEventListener("change", () => {
      if (selected.checked && state.selection.size >= state.limits.selection) {
        notify(`一次最多选择 ${state.limits.selection} 条语录，请分批操作。`);
      } else if (selected.checked) state.selection.set(quote.id, quote);
      else state.selection.delete(quote.id);
      updateSelection();
    });
    row.querySelector(".quote-text").textContent = quote.body;
    row.querySelector(".quote-meta").textContent = `#${quote.id} · ${displayDate(quote.created_at)}`;
    row.querySelector(".keyword-tag").textContent = quote.keyword;
    row.querySelector(".keyword-tag").classList.add("library-tone", `tone-${libraryTone(quote.keyword)}`);
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
  const filtered = Boolean(state.query || state.libraries.length || state.scope);
  $("empty-title").textContent = filtered ? "没有找到匹配的语录" : "还没有收录语录";
  $("empty-description").textContent = filtered ? "换个关键词，或清除筛选再试试。" : "先在 QQ 群中发送“添加 关键词#语录”，此处就会出现对应会话。";
  $("result-count").textContent = `共 ${state.total.toLocaleString("zh-CN")} 条语录`;
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  $("page-number").textContent = `${state.page} / ${pages}`;
  $("previous").disabled = state.page <= 1;
  $("next").disabled = state.page >= pages;
  updateSelection();
}

async function loadQuotes() {
  const sequence = ++listSequence;
  state.loading = true;
  state.listReady = false;
  updateSelection();
  $("table-region").setAttribute("aria-busy", "true");
  try {
    const data = await bridge.apiGet("ui/quotes", {
      scope: state.scope, keywords: JSON.stringify(state.libraries), query: state.query,
      page: state.page, page_size: state.pageSize,
    });
    if (sequence !== listSequence) return;
    state.items = data.items;
    state.total = data.total;
    state.listReady = true;
    const pages = Math.max(1, Math.ceil(data.total / state.pageSize));
    if (state.page > pages) {
      state.page = pages;
      return await loadQuotes();
    }
    renderRows();
  } catch (error) {
    if (sequence === listSequence) throw error;
  } finally {
    if (sequence === listSequence) {
      state.loading = false;
      $("table-region").setAttribute("aria-busy", "false");
      updateSelection();
    }
  }
}

async function refresh() {
  if (!state.ready) return start();
  listSequence++;
  state.listReady = false;
  clearSelection();
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
  clearSelection();
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
  $("quote-keyword").value = quote?.keyword || (state.libraries.length === 1 ? state.libraries[0] : "");
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
  $("delete-keyword").textContent = `库名：${quote.keyword} · ${quote.label}`;
  $("delete-dialog").showModal();
  $("delete-dialog").querySelector("[data-close]").focus();
}

$("select-page").addEventListener("change", () => {
  if (state.loading || !state.listReady) return updateSelection();
  if ($("select-page").checked) {
    const fresh = state.items.filter(quote => !state.selection.has(quote.id));
    if (state.selection.size + fresh.length > state.limits.selection) {
      notify(`一次最多选择 ${state.limits.selection} 条语录，请分批操作。`);
    } else for (const quote of fresh) state.selection.set(quote.id, quote);
  } else for (const quote of state.items) state.selection.delete(quote.id);
  updateSelection();
});
$("clear-selection").addEventListener("click", clearSelection);
$("batch-delete").addEventListener("click", () => {
  if (!state.selection.size || state.loading || !state.listReady || batchBusy) return;
  batchDeleting = [...state.selection.values()];
  message("batch-error", "");
  $("batch-dialog-title").textContent = `删除所选 ${batchDeleting.length} 条语录？`;
  const libraries = new Set(batchDeleting.map(quote => quote.keyword)).size;
  const scopes = new Set(batchDeleting.map(quote => quote.scope)).size;
  $("batch-description").textContent = `将删除下方 ${libraries} 个库、${scopes} 个会话中的所选语录。请核对内容，其他语录不受影响。`;
  const preview = document.createDocumentFragment();
  for (const quote of batchDeleting) {
    const item = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = `#${quote.id} · ${quote.keyword} · ${quote.label} · ${quote.detail}`;
    const body = document.createElement("p");
    body.textContent = quote.body.length > 160 ? quote.body.slice(0, 160) + "…" : quote.body;
    item.append(title, body);
    preview.append(item);
  }
  $("batch-preview").replaceChildren(preview);
  $("batch-dialog").showModal();
  $("batch-dialog").querySelector("[data-close]").focus();
});
$("batch-dialog").addEventListener("cancel", event => { if (batchBusy) event.preventDefault(); });
$("batch-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!batchDeleting.length || batchBusy) return;
  batchBusy = true;
  message("batch-error", "");
  for (const button of $("batch-form").querySelectorAll("button")) button.disabled = true;
  try {
    const result = await bridge.apiPost("ui/delete_many", {items: batchDeleting.map(quote => ({id: quote.id, version: quote.version}))});
    $("batch-dialog").close();
    clearSelection();
    notify(`已删除 ${result.deleted} 条语录。`);
    await refresh();
  } catch (error) {
    message("batch-error", errorText(error));
  } finally {
    batchBusy = false;
    for (const button of $("batch-form").querySelectorAll("button")) button.disabled = false;
  }
});

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
    state.listReady = false;
    clearSelection();
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
  state.listReady = false;
  clearSelection();
  if (state.ready) searchTimer = setTimeout(applyFilters, 250);
});
$("filters").addEventListener("submit", (event) => { event.preventDefault(); if (state.ready) applyFilters(); });
$("scope-filter").addEventListener("change", () => { if (state.ready) applyFilters(); });
$("library-search").addEventListener("input", renderLibraries);
$("clear-libraries").addEventListener("click", () => {
  state.libraries = [];
  $("library-search").value = "";
  renderLibraries();
  if (state.ready) applyFilters();
});
$("library-show-more").addEventListener("click", () => {
  state.librariesExpanded = !state.librariesExpanded;
  renderLibraries();
});
$("clear-filters").addEventListener("click", () => {
  $("search").value = $("scope-filter").value = $("library-search").value = "";
  state.libraries = [];
  renderLibraries();
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
