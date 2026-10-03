"use strict";
const $ = (s) => document.querySelector(s);
const labels = {
  pending: "待审核",
  approved: "已确认",
  published: "已发布",
  rejected: "已拒绝",
  retired: "已淘汰",
};
const fields = [
  "entity",
  "attribute",
  "value",
  "unit",
  "conditions",
  "valid_from",
  "valid_until",
];
const fieldNames = {
  entity: "主体",
  attribute: "属性",
  value: "事实值",
  unit: "单位",
  conditions: "适用条件",
  valid_from: "生效日期",
  valid_until: "失效日期",
};
const state = {
  csrf: "",
  libraries: [],
  library: null,
  tab: "facts",
  status: "pending",
  query: "",
  offset: 0,
  rows: [],
  selected: new Set(),
  settings: {},
};
function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "checked") node.checked = v;
    else if (k === "value") node.value = v;
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined)
      node.append(
        child instanceof Node ? child : document.createTextNode(String(child)),
      );
  }
  return node;
}
const sidebarPreference = "fact-manager.sidebar-collapsed";
function setSidebarCollapsed(collapsed, persist = false) {
  $("#workspace").classList.toggle("sidebar-collapsed", collapsed);
  const button = $("#sidebar-toggle");
  const label = collapsed ? "展开侧边栏" : "收起侧边栏";
  button.setAttribute("aria-expanded", String(!collapsed));
  button.setAttribute("aria-label", label);
  button.title = label;
  if (persist) {
    try {
      localStorage.setItem(sidebarPreference, String(collapsed));
    } catch {}
  }
}
function initializeSidebar() {
  let preference = null;
  try {
    preference = localStorage.getItem(sidebarPreference);
  } catch {}
  setSidebarCollapsed(
    preference === null
      ? matchMedia("(max-width: 720px)").matches
      : preference === "true",
  );
}
let factTableObserver = null;
let activeHint = null;
let hintCounter = 0;
function hideHint() {
  if (activeHint) activeHint.hidden = true;
  activeHint = null;
}
function hintedButton(label, description, attrs = {}) {
  const id = `action-hint-${++hintCounter}`;
  const tip = h(
    "span",
    { class: "action-tooltip", id, role: "tooltip", hidden: "" },
    description,
  );
  const button = h("button", { ...attrs, "aria-describedby": id }, label);
  const wrapper = h(
    "span",
    {
      class: "action-hint",
      ...(attrs.disabled !== undefined
        ? { tabindex: "0", "aria-label": label, "aria-describedby": id }
        : {}),
    },
    button,
    tip,
  );
  const show = () => {
    hideHint();
    tip.hidden = false;
    activeHint = tip;
    const rect = wrapper.getBoundingClientRect();
    const gap = 10;
    const size = tip.getBoundingClientRect();
    tip.style.left =
      Math.max(gap, Math.min(rect.left, window.innerWidth - size.width - gap)) +
      "px";
    tip.style.top =
      (rect.bottom + size.height + gap * 2 <= window.innerHeight
        ? rect.bottom + gap
        : Math.max(gap, rect.top - size.height - gap)) + "px";
  };
  wrapper.addEventListener("mouseenter", show);
  wrapper.addEventListener("mouseleave", hideHint);
  wrapper.addEventListener("focusin", show);
  wrapper.addEventListener("focusout", hideHint);
  return wrapper;
}
function notice(message, error = false) {
  const n = $("#notice");
  n.textContent = message;
  n.className = "notice" + (error ? " error" : "");
  n.hidden = false;
  if (error && $("#dialog").open) {
    let alert = $("#dialog-body .dialog-error");
    if (!alert) {
      alert = h("div", { class: "notice error dialog-error", role: "alert" });
      $("#dialog-body").prepend(alert);
    }
    alert.textContent = message;
  }
}
function busy(button, fn) {
  $("#dialog-body .dialog-error")?.remove();
  button.disabled = true;
  return Promise.resolve()
    .then(fn)
    .catch((e) => notice(e.message, true))
    .finally(() => (button.disabled = false));
}
async function api(path, method = "GET", body = null) {
  const options = { method, headers: {} };
  if (method !== "GET") options.headers["X-Fact-CSRF"] = state.csrf;
  if (body instanceof FormData) options.body = body;
  else if (body !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch("/api/" + path, options);
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) showLogin();
    throw new Error(data.error || "请求失败");
  }
  return data;
}
const libPath = () => `libraries/${state.library.id}`;
function showLogin() {
  $("#workspace").hidden = true;
  $("#login").hidden = false;
  state.csrf = "";
}
function dialog(title, ...contents) {
  hideHint();
  $("#dialog-title").textContent = title;
  $("#dialog-body").replaceChildren(...contents);
  $("#dialog").showModal();
}
function closeDialog() {
  $("#dialog").close();
  $("#dialog-body").replaceChildren();
}
function inputField(name, label, value = "", tag = "input", attrs = {}) {
  const control = h(tag, { name, value, ...attrs });
  return [h("label", {}, label, control), control];
}
async function initialize() {
  state.settings = await api("settings");
  $("#ragflow-link").href = state.settings.ragflow_url;
  $("#login").hidden = true;
  $("#workspace").hidden = false;
  await refreshLibraries();
}
async function refreshLibraries() {
  state.libraries = await api("libraries");
  if (state.library)
    state.library =
      state.libraries.find((l) => l.id === state.library.id) || null;
  if (!state.library && state.libraries.length)
    state.library =
      state.libraries.find((l) => l.id === location.hash.slice(1)) ||
      state.libraries[0];
  renderSidebar();
  await refreshContent();
}
function renderSidebar() {
  $("#library-list").replaceChildren(
    ...state.libraries.map((l) =>
      h(
        "button",
        {
          class:
            "library-item" + (state.library?.id === l.id ? " selected" : ""),
          title: l.name,
          "aria-label": l.name,
          "aria-current": state.library?.id === l.id ? "true" : "false",
          onclick: async () => {
            state.library = l;
            location.hash = l.id;
            state.offset = 0;
            state.selected.clear();
            renderSidebar();
            await refreshContent();
          },
        },
        h(
          "span",
          { class: "library-initial", "aria-hidden": "true" },
          Array.from(l.name.trim()).slice(0, 2).join("").toLocaleUpperCase(),
        ),
        h("span", { class: "library-name" }, l.name),
      ),
    ),
  );
}
async function refreshContent() {
  hideHint();
  factTableObserver?.disconnect();
  factTableObserver = null;
  const l = state.library;
  $("#notice").hidden = true;
  if (!l) {
    $("#content").replaceChildren(
      h(
        "div",
        { class: "empty" },
        h("h2", {}, "创建第一个事实库"),
        h("p", {}, "为一个主题收集来源、提取候选事实，并逐步审核发布。"),
        h("button", { class: "primary", onclick: createLibrary }, "新建事实库"),
      ),
    );
    return;
  }
  state.library = await api(`libraries/${l.id}`);
  $("#library-title").textContent = state.library.name;
  $("#library-description").textContent = state.library.description;
  $("#statistics").replaceChildren(
    ...[
      ["来源", "sources"],
      ["待审核", "pending"],
      ["已确认", "approved"],
      ["已发布", "published"],
    ].map(([label, key]) =>
      h(
        "div",
        { class: "stat" },
        h("span", {}, label),
        h("strong", {}, state.library.statistics[key]),
      ),
    ),
  );
  $("#tabs").replaceChildren(
    ...[
      ["facts", "事实与审核"],
      ["sources", "来源与提取"],
      ["checks", "检查与推导"],
      ["tokens", "MCP 访问"],
      ["settings", "事实库设置"],
    ].map(([key, label]) =>
      h(
        "button",
        {
          class: "tab" + (key === state.tab ? " selected" : ""),
          onclick: async () => {
            state.tab = key;
            state.offset = 0;
            state.selected.clear();
            await refreshContent();
          },
        },
        label,
      ),
    ),
  );
  const render = {
    facts: renderFacts,
    sources: renderSources,
    checks: renderChecks,
    tokens: renderTokens,
    settings: renderSettings,
  }[state.tab];
  await render();
}
function createLibrary() {
  const [nameLabel, name] = inputField("name", "事实库名称");
  const [descLabel, description] = inputField(
    "description",
    "用途说明",
    "",
    "textarea",
  );
  const button = h(
    "button",
    {
      class: "primary",
      onclick: () =>
        busy(button, async () => {
          const library = await api("libraries", "POST", {
            name: name.value,
            description: description.value,
          });
          state.library = library;
          state.tab = "sources";
          closeDialog();
          await refreshLibraries();
        }),
    },
    "创建事实库",
  );
  dialog(
    "新建事实库",
    h(
      "p",
      { class: "muted" },
      "每个事实库拥有独立的来源、提取指令、审核记录和 MCP Token。",
    ),
    nameLabel,
    descLabel,
    h("div", { class: "dialog-actions" }, button),
  );
}
function reviewerDialog(title, action, external = false) {
  const [label, reviewer] = inputField("reviewer", "审核／操作人", "管理员");
  const check = h("input", { type: "checkbox" });
  const button = h(
    "button",
    {
      class: "primary",
      onclick: () =>
        busy(button, async () => {
          await action(reviewer.value, check.checked);
          closeDialog();
          await refreshLibraries();
        }),
    },
    "确认",
  );
  dialog(
    title,
    label,
    ...(external
      ? [h("label", { class: "checklabel" }, check, "允许所选事实用于外部引用")]
      : []),
    h("div", { class: "dialog-actions" }, button),
  );
}
async function renderFacts() {
  factTableObserver?.disconnect();
  factTableObserver = null;
  const base = libPath();
  const result = await api(
    base +
      "/facts?" +
      new URLSearchParams({
        status: state.status,
        query: state.query,
        offset: state.offset,
        limit: 50,
      }),
  );
  state.rows = result.rows;
  state.selected.clear();
  const select = h(
    "select",
    {
      onchange: async (e) => {
        state.status = e.target.value;
        state.offset = 0;
        await renderFacts();
      },
    },
    h("option", { value: "" }, "全部状态"),
    ...Object.entries(labels).map(([v, l]) => h("option", { value: v }, l)),
  );
  select.value = state.status;
  const search = h("input", {
    type: "search",
    placeholder: "搜索主体、属性或事实值",
    value: state.query,
    onkeydown: async (e) => {
      if (e.key === "Enter") {
        state.query = e.target.value;
        state.offset = 0;
        await renderFacts();
      }
    },
  });
  const approve = h(
    "button",
    { disabled: "", onclick: () => batchReview("approve") },
    "批量确认",
  );
  const reject = h(
    "button",
    { disabled: "", onclick: () => batchReview("reject") },
    "批量拒绝",
  );
  const updateSelection = () => {
    approve.disabled = reject.disabled = !state.selected.size;
  };
  const selectAll = h("input", {
    type: "checkbox",
    "aria-label": "选择本页待审核事实",
    onchange: (e) => {
      state.rows
        .filter((r) => r.status === "pending" && r.source_active)
        .forEach((r) => {
          if (e.target.checked) state.selected.add(r.id);
          else state.selected.delete(r.id);
        });
      $("#content")
        .querySelectorAll("input[data-fact]")
        .forEach((c) => (c.checked = state.selected.has(c.dataset.fact)));
      updateSelection();
    },
  });
  const rows = result.rows.map((r) => {
    const checkbox = h("input", {
      type: "checkbox",
      "data-fact": r.id,
      "aria-label": "选择事实",
      onchange: (e) => {
        e.target.checked
          ? state.selected.add(r.id)
          : state.selected.delete(r.id);
        updateSelection();
      },
    });
    checkbox.disabled = r.status !== "pending" || !r.source_active;
    return h(
      "tr",
      {},
      h("td", {}, checkbox),
      h("td", { class: "fact-entity" }, r.entity),
      h("td", { class: "fact-attribute" }, r.attribute),
      h(
        "td",
        { class: "fact-value" },
        r.value + (r.unit ? " " + r.unit : ""),
        r.conditions ? h("div", { class: "fact-meta" }, r.conditions) : null,
        r.valid_from || r.valid_until
          ? h(
              "div",
              { class: "fact-meta" },
              `${r.valid_from || "未限定"} → ${r.valid_until || "未限定"}`,
            )
          : null,
      ),
      h(
        "td",
        { class: "fact-quote" },
        h("div", { class: "quote-preview" }, r.quote),
        h("div", { class: "fact-meta" }, r.name),
      ),
      h(
        "td",
        { class: "fact-status" },
        h("span", { class: "badge " + r.status }, labels[r.status]),
        checkBadge(r),
        r.origin?.kind === "derived"
          ? h(
              "div",
              { class: "fact-meta" },
              r.origin.current ? "推导候选／结论" : "推导失效 · 停止提供",
            )
          : null,
        r.external_use
          ? h("div", { class: "fact-meta" }, "允许外部引用")
          : null,
      ),
      h(
        "td",
        { class: "fact-actions" },
        h(
          "div",
          { class: "actions" },
          h(
            "button",
            { class: "quiet", onclick: () => reviewFact(r) },
            r.status === "pending" ? "审核" : "详情",
          ),
          ["approved", "published"].includes(r.status)
            ? h(
                "button",
                { class: "quiet danger", onclick: () => retireFact(r) },
                "淘汰",
              )
            : null,
        ),
      ),
    );
  });
  const publish = h(
    "button",
    {
      class: "primary",
      onclick: () =>
        reviewerDialog("发布当前库的已确认事实", async (reviewer) => {
          const r = await api(base + "/publish", "POST", { reviewer });
          notice(`已发布 ${r.published} 条事实。`);
        }),
    },
    "发布已确认",
  );
  publish.disabled = !state.library.statistics.approved;
  const importInput = h("input", {
    type: "file",
    accept: ".xlsx",
    onchange: (e) => importWorkbook(e.target.files[0]),
  });
  const toolbar = h(
    "div",
    { class: "toolbar" },
    select,
    search,
    approve,
    reject,
    h("span", { class: "spacer" }),
    h("a", { href: "/api/" + base + "/review.xlsx" }, "导出 XLSX"),
    h("label", { class: "upload-label" }, "导入审核表", importInput),
    publish,
  );
  const table = h(
    "div",
    {
      class: "table-wrap",
      role: "region",
      "aria-label": "事实审核表格，可横向滚动",
      tabindex: "0",
    },
    h(
      "table",
      { class: "facts-table" },
      h(
        "colgroup",
        {},
        h("col", { class: "fact-select-column" }),
        h("col", { class: "fact-entity-column" }),
        h("col", { class: "fact-attribute-column" }),
        h("col", {}),
        h("col", { class: "fact-quote-column" }),
        h("col", { class: "fact-status-column" }),
        h("col", { class: "fact-actions-column" }),
      ),
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          h("th", { scope: "col" }, selectAll),
          h("th", { class: "fact-entity" }, "主体"),
          h("th", { class: "fact-attribute" }, "属性"),
          h("th", {}, "事实值与条件"),
          h("th", { class: "fact-quote" }, "原文证据"),
          h("th", { class: "fact-status" }, "状态"),
          h("th", { class: "fact-actions", scope: "col" }, "操作"),
        ),
      ),
      h("tbody", {}, rows),
    ),
  );
  const scrollHint = h(
    "p",
    { class: "table-scroll-hint", hidden: "" },
    "表格可左右滚动，操作按钮保持在右侧。",
  );
  const pagination = h(
    "div",
    { class: "pagination" },
    `${result.total} 条 · 每页 50 条`,
    h(
      "div",
      { class: "actions" },
      h(
        "button",
        {
          onclick: async () => {
            state.offset = Math.max(0, state.offset - 50);
            await renderFacts();
          },
          ...(state.offset === 0 ? { disabled: "" } : {}),
        },
        "上一页",
      ),
      h(
        "button",
        {
          onclick: async () => {
            state.offset += 50;
            await renderFacts();
          },
          ...(state.offset + 50 >= result.total ? { disabled: "" } : {}),
        },
        "下一页",
      ),
    ),
  );
  $("#content").replaceChildren(
    toolbar,
    ...(rows.length
      ? [scrollHint, table, pagination]
      : [
          h(
            "div",
            { class: "empty" },
            "当前筛选下没有事实。可以在“来源与提取”中添加来源并启动提取。",
          ),
        ]),
  );
  if (rows.length) {
    factTableObserver = new ResizeObserver(() => {
      scrollHint.hidden = table.scrollWidth <= table.clientWidth + 1;
    });
    factTableObserver.observe(table);
  }
}
function reviewChange(r, values, decision, external) {
  return {
    ...r,
    ...values,
    fingerprint: r.fingerprint,
    source_id: r.source_id,
    chunk_id: r.chunk_id,
    quote: r.quote,
    file_sha256: r.file_hash,
    decision,
    external_use: external ? "yes" : "no",
  };
}
function batchReview(decision) {
  const rows = state.rows.filter((r) => state.selected.has(r.id));
  reviewerDialog(
    `${decision === "approve" ? "确认" : "拒绝"} ${rows.length} 条候选事实`,
    async (reviewer, external) => {
      await api(libPath() + "/review", "POST", {
        reviewer,
        changes: rows.map((r) => reviewChange(r, {}, decision, external)),
      });
    },
    decision === "approve",
  );
}
function reviewFact(r) {
  const editable = r.status === "pending";
  const controls = {};
  const nodes = [];
  for (const field of fields) {
    const [label, input] = inputField(
      field,
      fieldNames[field],
      r[field] || "",
      ["value", "conditions"].includes(field) ? "textarea" : "input",
      field.startsWith("valid_") ? { type: "date" } : {},
    );
    input.disabled = !editable || r.origin?.kind === "derived";
    controls[field] = input;
    nodes.push(label);
  }
  const external = h("input", { type: "checkbox", checked: !!r.external_use });
  external.disabled = !editable;
  const [reviewerLabel, reviewer] = inputField("reviewer", "审核人", "管理员");
  const submit = (decision) => {
    const button = h(
      "button",
      {
        class: decision === "approve" ? "primary" : "danger",
        onclick: () =>
          busy(button, async () => {
            const values = Object.fromEntries(
              fields.map((f) => [f, controls[f].value]),
            );
            await api(libPath() + "/review", "POST", {
              reviewer: reviewer.value,
              changes: [reviewChange(r, values, decision, external.checked)],
            });
            closeDialog();
            await refreshLibraries();
          }),
      },
      decision === "approve" ? "确认事实" : "拒绝候选",
    );
    return button;
  };
  const auditButton = h(
    "button",
    {
      onclick: async () => {
        const events = await api(libPath() + `/facts/${r.id}/audit`);
        dialog(
          "事实审核记录",
          h(
            "pre",
            {},
            events.length ? JSON.stringify(events, null, 2) : "尚无审核记录。",
          ),
        );
      },
    },
    "查看审核记录",
  );
  dialog(
    editable ? "核对并审核事实" : "事实详情",
    h("p", { class: "fine" }, `来源：${r.name}`),
    h(
      "p",
      { class: "fine" },
      r.origin?.kind === "derived"
        ? "以下为前提的原文引用；它并非直接披露推导结论。"
        : "直接来源原文引用",
    ),
    h("div", { class: "evidence" }, r.quote),
    h("button", { onclick: () => inspectEvidence(r) }, "核对原文件与解析结构"),
    h(
      "p",
      { class: "fine" },
      "原文证据保持只读。可修正事实值、条件和日期；无法由证据支持的内容请拒绝。",
    ),
    checkDetails(r, controls, editable),
    ...nodes,
    h("label", { class: "checklabel" }, external, "允许用于外部引用"),
    ...(editable
      ? [
          reviewerLabel,
          h(
            "div",
            { class: "dialog-actions" },
            submit("reject"),
            submit("approve"),
          ),
        ]
      : [h("div", { class: "dialog-actions" }, auditButton)]),
  );
}
function retireFact(r) {
  const [label, reason] = inputField("reason", "淘汰原因", "", "textarea");
  const [reviewerLabel, reviewer] = inputField("reviewer", "操作人", "管理员");
  const button = h(
    "button",
    {
      class: "danger",
      onclick: () =>
        busy(button, async () => {
          await api(libPath() + `/facts/${r.id}/retire`, "POST", {
            reviewer: reviewer.value,
            reason: reason.value,
          });
          closeDialog();
          await refreshLibraries();
        }),
    },
    "确认淘汰",
  );
  dialog(
    "淘汰事实",
    h("div", { class: "evidence" }, `${r.entity} · ${r.attribute}：${r.value}`),
    label,
    reviewerLabel,
    h("div", { class: "dialog-actions" }, button),
  );
}
function importWorkbook(file) {
  if (!file) return;
  reviewerDialog("导入审核工作簿", async (reviewer) => {
    const form = new FormData();
    form.append("file", file);
    form.append("reviewer", reviewer);
    const result = await api(libPath() + "/review-import", "POST", form);
    notice(`已导入 ${result.reviewed} 条审核结果。`);
  });
}
async function startJob(kind) {
  if (
    ["extract", "check"].includes(kind) &&
    !confirm(
      kind === "check"
        ? "将使用配置的模型检查尚未完成检查的候选，可能产生 API 费用。不会自动审批。是否启动？"
        : "将使用配置的模型提取候选事实，可能产生 API 费用。是否启动？",
    )
  )
    return;
  await api(libPath() + "/jobs", "POST", { kind });
  await refreshContent();
}
async function renderSources() {
  const base = libPath();
  const [sources, jobs] = await Promise.all([
    api(base + "/sources"),
    api(base + "/jobs"),
  ]);
  const active = jobs.some((j) => ["queued", "running"].includes(j.status));
  state.jobActive = active;
  const toolbar = h(
    "div",
    { class: "toolbar" },
    h("button", { class: "primary", onclick: () => textSource() }, "录入文字"),
    h(
      "label",
      { class: "upload-label" },
      "上传 TXT／Markdown",
      h("input", {
        type: "file",
        accept: ".txt,.md,.markdown",
        onchange: async (e) => {
          if (!e.target.files[0]) return;
          const form = new FormData();
          form.append("file", e.target.files[0]);
          try {
            await api(base + "/sources/upload", "POST", form);
            await refreshLibraries();
          } catch (error) {
            notice(error.message, true);
          }
        },
      }),
    ),
    h("button", { onclick: bindDataset }, "绑定 RAGFlow Dataset"),
    h("span", { class: "spacer" }),
    h(
      "button",
      {
        ...(active ? { disabled: "" } : {}),
        onclick: () => startJob("sync").catch((e) => notice(e.message, true)),
      },
      "同步来源",
    ),
    h(
      "button",
      {
        class: "primary",
        ...(active || !sources.length ? { disabled: "" } : {}),
        onclick: () =>
          startJob("extract").catch((e) => notice(e.message, true)),
      },
      "提取候选事实",
    ),
  );
  const bindings = h(
    "div",
    { class: "card" },
    h("h3", {}, "RAGFlow 数据集"),
    h(
      "p",
      { class: "fine" },
      "PDF、Word 等文件在 RAGFlow 上传解析，再绑定 Dataset。同步只保存证据，不调用提取模型。",
    ),
    ...state.library.dataset_ids.map((id) =>
      h(
        "div",
        { class: "binding" },
        h("code", {}, id),
        h(
          "button",
          {
            class: "quiet danger",
            onclick: async () => {
              if (
                confirm(
                  "取消绑定会立即停止提供该 Dataset 的事实和证据，历史记录仍保留。",
                )
              ) {
                await api(base + "/bindings/" + id, "DELETE");
                await refreshLibraries();
              }
            },
          },
          "取消绑定",
        ),
      ),
    ),
  );
  const cards = sources.map((source) =>
    h(
      "div",
      { class: "card" },
      h("div", { class: "source-title" }, source.name),
      h(
        "div",
        { class: "source-meta" },
        `${source.dataset_id === "local" ? "本地文本" : "RAGFlow 来源"} · ${new Date(source.created_at).toLocaleString()} · SHA256 ${source.file_hash.slice(0, 16)}…`,
      ),
      h(
        "div",
        { class: "actions" },
        h(
          "button",
          { onclick: () => sourceChunks(source) },
          "查看证据／添加事实",
        ),
        h(
          "a",
          { href: "/api/" + base + `/sources/${source.id}/original` },
          "下载原文件",
        ),
        source.dataset_id === "local"
          ? h("button", { onclick: () => editTextSource(source) }, "编辑新版本")
          : null,
        h(
          "button",
          {
            class: "danger quiet",
            onclick: async () => {
              if (
                confirm("停用后该来源的事实和证据将不再提供，历史文件仍保留。")
              ) {
                await api(base + "/sources/" + source.id, "DELETE");
                await refreshLibraries();
              }
            },
          },
          "停用来源",
        ),
      ),
    ),
  );
  const jobSection = h(
    "div",
    { class: "card" },
    h("h3", {}, "提取与同步任务"),
    ...jobs
      .slice(0, 5)
      .map((job) =>
        h(
          "div",
          { class: "job" },
          `${jobNames[job.kind] || job.kind} · ${new Date(job.created_at).toLocaleString()} · ${{ queued: "排队中", running: "运行中", succeeded: "已完成", failed: "失败" }[job.status]}`,
          job.total ? ` · ${job.progress}/${job.total} 分块` : null,
          job.result ? jobResult(job.result) : null,
          job.error ? h("div", { class: "job-error" }, job.error) : null,
        ),
      ),
    !jobs.length
      ? h(
          "p",
          { class: "fine" },
          "尚无任务。已完成的提取分块会跳过，失败任务可重新启动。",
        )
      : null,
  );
  $("#content").replaceChildren(
    toolbar,
    bindings,
    ...cards,
    ...(!sources.length
      ? [
          h(
            "div",
            { class: "empty" },
            "尚无可用来源。添加文本，或绑定 Dataset 后同步。",
          ),
        ]
      : []),
    jobSection,
  );
}
function textSource(source = null, text = "") {
  const [label, name] = inputField("name", "来源名称", source?.name || "");
  const [textLabel, content] = inputField(
    "text",
    "来源正文",
    text,
    "textarea",
    { rows: 13 },
  );
  const button = h(
    "button",
    {
      class: "primary",
      onclick: () =>
        busy(button, async () => {
          await api(libPath() + "/sources/text", "POST", {
            name: name.value,
            text: content.value,
            document_id: source?.document_id,
          });
          closeDialog();
          await refreshLibraries();
        }),
    },
    source ? "保存新版本" : "添加来源",
  );
  dialog(
    source ? "更新文本来源" : "录入文本来源",
    label,
    textLabel,
    h(
      "p",
      { class: "fine" },
      "新版本保存后，旧版本的事实将停止提供，需重新提取与审核。",
    ),
    h("div", { class: "dialog-actions" }, button),
  );
}
async function editTextSource(source) {
  const response = await fetch(
    "/api/" + libPath() + `/sources/${source.id}/original`,
  );
  if (!response.ok) return notice("无法读取原文件。", true);
  textSource(source, await response.text());
}
async function bindDataset() {
  const [label, dataset] = inputField("dataset_id", "Dataset ID");
  const select = h(
    "select",
    {},
    h("option", { value: "" }, "选择可访问的 RAGFlow Dataset"),
  );
  const button = h(
    "button",
    {
      class: "primary",
      onclick: () =>
        busy(button, async () => {
          await api(libPath() + "/bindings", "POST", {
            dataset_id: dataset.value,
          });
          closeDialog();
          await refreshLibraries();
        }),
    },
    "绑定",
  );
  select.addEventListener("change", () => (dataset.value = select.value));
  dialog(
    "绑定 RAGFlow Dataset",
    h(
      "p",
      { class: "muted" },
      "同一个 Dataset 可以作为多个事实库的来源。每个库独立审核和发布。",
    ),
    select,
    label,
    h("div", { class: "dialog-actions" }, button),
  );
  try {
    const rows = await api("ragflow/datasets");
    select.append(...rows.map((r) => h("option", { value: r.id }, r.name)));
  } catch (error) {
    select.replaceChildren(
      h("option", { value: "" }, "列表读取失败，请手动填写 ID"),
    );
  }
}
async function sourceChunks(source) {
  const chunks = await api(libPath() + `/sources/${source.id}/chunks`);
  dialog(
    source.name,
    ...chunks.map((chunk) =>
      h(
        "section",
        { class: "chunk" },
        h(
          "div",
          { class: "section-title" },
          h("h3", {}, "证据分块 " + chunk.id),
          h(
            "button",
            { class: "quiet", onclick: () => manualFact(source, chunk) },
            "添加候选事实",
          ),
        ),
        h("pre", {}, chunk.content),
      ),
    ),
  );
}
function manualFact(source, chunk) {
  const controls = {};
  const nodes = [];
  for (const field of fields) {
    const [label, input] = inputField(
      field,
      fieldNames[field],
      "",
      ["value", "conditions"].includes(field) ? "textarea" : "input",
      field.startsWith("valid_") ? { type: "date" } : {},
    );
    controls[field] = input;
    nodes.push(label);
  }
  const [quoteLabel, quote] = inputField(
    "quote",
    "逐字原文引用",
    "",
    "textarea",
  );
  const button = h(
    "button",
    {
      class: "primary",
      onclick: () =>
        busy(button, async () => {
          await api(libPath() + "/facts", "POST", {
            source_id: source.id,
            chunk_id: chunk.id,
            fact: {
              ...Object.fromEntries(fields.map((f) => [f, controls[f].value])),
              quote: quote.value,
            },
          });
          state.tab = "facts";
          state.status = "pending";
          closeDialog();
          await refreshLibraries();
        }),
    },
    "添加待审核事实",
  );
  dialog(
    "手动添加候选事实",
    h("div", { class: "evidence" }, chunk.content),
    ...nodes,
    quoteLabel,
    h(
      "p",
      { class: "fine" },
      "引用必须是上方证据中连续、逐字的原文。添加后仍需审核与发布。",
    ),
    h("div", { class: "dialog-actions" }, button),
  );
}
async function renderTokens() {
  const tokens = await api(libPath() + "/tokens");
  const [nameLabel, name] = inputField("name", "Token 名称", "", "input", {
    placeholder: "例如：Hermes 项目申报助手",
  });
  const [daysLabel, days] = inputField(
    "expires_days",
    "有效天数（留空为不限期）",
    "",
    "input",
    { type: "number", min: 1, max: 3650 },
  );
  const scope = h(
    "select",
    { name: "scope", "aria-label": "权限" },
    h("option", { value: "read" }, "只读：查询已发布事实与证据"),
    h("option", { value: "review" }, "检查：读取候选、提交检查与推导建议"),
  );
  const scopeLabel = h("label", {}, "权限", scope);
  const generate = h(
    "button",
    {
      class: "primary",
      onclick: () =>
        busy(generate, async () => {
          const data = await api(libPath() + "/tokens", "POST", {
            name: name.value,
            expires_days: days.value ? Number(days.value) : null,
            scope: scope.value,
          });
          showToken(data);
          await renderTokens();
        }),
    },
    "生成 MCP Token",
  );
  const form = h(
    "div",
    { class: "card" },
    h("h3", {}, "为此事实库生成访问 Token"),
    h(
      "p",
      { class: "muted" },
      "每个 Token 只访问当前库。只读权限用于取用事实；检查权限可提交检查与推导候选。审批、发布和管理仍需网页登录。",
    ),
    h("div", { class: "row" }, nameLabel, daysLabel, scopeLabel),
    generate,
  );
  const rows = tokens.map((t) =>
    h(
      "tr",
      {},
      h("td", {}, t.name, h("div", { class: "fine" }, t.prefix + "…")),
      h("td", {}, t.scope === "review" ? "检查" : "只读"),
      h("td", {}, new Date(t.created_at).toLocaleString()),
      h(
        "td",
        {},
        t.expires_at ? new Date(t.expires_at).toLocaleDateString() : "不限期",
      ),
      h(
        "td",
        {},
        t.revoked_at
          ? "已撤销"
          : t.expires_at && new Date(t.expires_at) < new Date()
            ? "已过期"
            : "可用",
      ),
      h(
        "td",
        {},
        !t.revoked_at
          ? h(
              "button",
              {
                class: "quiet danger",
                onclick: async () => {
                  if (
                    confirm("撤销后使用此 Token 的智能体会立即失去访问权限。")
                  ) {
                    await api(libPath() + "/tokens/" + t.id, "DELETE");
                    await renderTokens();
                  }
                },
              },
              "撤销",
            )
          : null,
      ),
    ),
  );
  const guide = h(
    "div",
    { class: "card" },
    h("h3", {}, "MCP 连接配置"),
    h(
      "p",
      { class: "fine" },
      "使用 Streamable HTTP。请在智能体的运行时配置中填入本库生成的 Token。",
    ),
    h(
      "pre",
      {},
      JSON.stringify(
        {
          mcpServers: {
            fact_manager: {
              url: state.settings.mcp_url,
              headers: { Authorization: "Bearer <本库的 MCP Token>" },
            },
          },
        },
        null,
        2,
      ),
    ),
    h(
      "p",
      { class: "fine" },
      "只读工具：get_library、get_verified_facts、search_evidence、read_source、inspect_source。检查权限另外开放 get_review_packet、submit_fact_check、propose_derived_fact。管理员 Token 不用于 MCP。",
    ),
  );
  $("#content").replaceChildren(
    form,
    h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        {},
        h(
          "thead",
          {},
          h(
            "tr",
            {},
            ...["名称", "权限", "创建时间", "有效期", "状态", "操作"].map((n) =>
              h("th", {}, n),
            ),
          ),
        ),
        h("tbody", {}, rows),
      ),
    ),
    guide,
  );
}
function showToken(data) {
  const code = h("div", { class: "token-value" }, data.token);
  const copy = h(
    "button",
    {
      class: "primary",
      onclick: async () => {
        try {
          await navigator.clipboard.writeText(data.token);
          copy.textContent = "已复制";
        } catch (e) {
          copy.textContent = "请手动选择复制";
        }
      },
    },
    "复制 Token",
  );
  dialog(
    "MCP Token 已生成",
    h(
      "p",
      { class: "error" },
      "完整 Token 仅在本次显示，请立即复制并妥善保存。关闭后无法再次查看，可撤销并重新生成。",
    ),
    code,
    h(
      "div",
      { class: "dialog-actions" },
      copy,
      h("button", { onclick: closeDialog }, "我已保存"),
    ),
  );
}
async function renderSettings() {
  const l = state.library;
  const [nameLabel, name] = inputField("name", "事实库名称", l.name);
  const [descLabel, description] = inputField(
    "description",
    "用途说明",
    l.description,
    "textarea",
  );
  const [promptLabel, prompt] = inputField(
    "prompt",
    "候选事实提取 Prompt",
    l.prompt,
    "textarea",
    { class: "setting-prompt" },
  );
  const button = h(
    "button",
    {
      class: "primary",
      onclick: () =>
        busy(button, async () => {
          await api(libPath(), "PATCH", {
            name: name.value,
            description: description.value,
            prompt: prompt.value,
          });
          await refreshLibraries();
          notice("事实库设置已保存。");
        }),
    },
    "保存设置",
  );
  $("#content").replaceChildren(
    h(
      "div",
      { class: "card" },
      nameLabel,
      descLabel,
      promptLabel,
      h(
        "p",
        { class: "fine" },
        `模型：${state.settings.llm_model}。修改 Prompt 会生成新提取版本；下次手动启动提取时会按新版本处理，不会自动调用收费模型。`,
      ),
      h(
        "p",
        { class: "fine" },
        "请保留 JSON 输出字段和逐字证据引用要求。审核通过的事实仍须单独发布。",
      ),
      button,
    ),
  );
}

function jobResult(raw) {
  let r;
  try {
    r = typeof raw === "string" ? JSON.parse(raw) : raw;
  } catch {
    return h("p", { class: "fine" }, String(raw));
  }
  const nodes = [];
  const names = {
    snapshotted_documents: "已同步文档",
    new_candidates: "新增候选",
    checked: "本次检查",
    skipped: "已检查并跳过",
    flagged: "本地规则提示疑点",
  };
  for (const [k, label] of Object.entries(names))
    if (r[k] !== undefined)
      nodes.push(h("p", { class: "fine" }, `${label}：${r[k]}`));
  if (r.verdicts)
    nodes.push(
      h(
        "p",
        { class: "fine" },
        Object.entries(r.verdicts)
          .map(([k, v]) => `${verdictNames[k] || k} ${v} 条`)
          .join(" · "),
      ),
    );
  if (r.source_warnings)
    for (const source of r.source_warnings)
      nodes.push(
        h(
          "p",
          { class: "fine" },
          `${source.name}：${source.warnings.join("；")}`,
        ),
      );
  if (r.local_check)
    nodes.push(
      h("div", {}, h("strong", {}, "自动本地检查"), jobResult(r.local_check)),
    );
  return h("div", {}, ...nodes);
}
const verdictNames = {
  supported: "证据支持",
  needs_review: "需核对",
  contradicted: "与已审批前提冲突",
  insufficient_evidence: "证据不足",
};
const jobNames = {
  sync: "来源同步",
  extract: "候选事实提取",
  check: "模型语义检查",
  "check-local": "本地结构检查",
};
function checkBadge(r) {
  const c = r.check;
  return h(
    "div",
    { class: "fact-meta" },
    !c
      ? "尚未检查"
      : !c.current
        ? "检查过期 · 须重查"
        : c.kind === "local"
          ? "本地检查 · 待语义核验"
          : verdictNames[c.verdict],
  );
}
function checkDetails(r, controls, editable) {
  const c = r.check;
  const origin = r.origin;
  const nodes = [h("h3", {}, "检查意见（不等于审批）")];
  if (c) {
    nodes.push(
      h(
        "p",
        {},
        `${c.checker} · ${c.kind === "local" ? "本地结构检查" : verdictNames[c.verdict]} · ${c.current ? "当前版本" : "已过期"}`,
      ),
      h("div", { class: "evidence" }, c.summary),
    );
    if (!c.current)
      nodes.push(h("p", { class: "error" }, c.stale_reasons.join("；")));
    if (c.premises.length)
      nodes.push(
        h(
          "p",
          { class: "fine" },
          "引用已审批前提：" + c.premises.map((p) => p.id).join("、"),
        ),
      );
    if (Object.keys(c.suggestions).length) {
      nodes.push(h("pre", {}, JSON.stringify(c.suggestions, null, 2)));
      if (editable && c.current && origin?.kind !== "derived")
        nodes.push(
          h(
            "button",
            {
              onclick: () => {
                for (const [k, v] of Object.entries(c.suggestions))
                  if (controls[k]) controls[k].value = v;
              },
            },
            "将建议填入表单（仍须确认）",
          ),
        );
    }
  } else
    nodes.push(
      h(
        "p",
        { class: "fine" },
        "尚无检查意见，可在“检查与推导”启动模型检查，或让持检查 Token 的智能体核验。",
      ),
    );
  if (origin?.kind === "derived") {
    nodes.push(
      h("h3", {}, "推导依据"),
      h("p", {}, origin.certainty),
      h("div", { class: "evidence" }, origin.reasoning),
    );
    for (const p of origin.premises)
      nodes.push(
        h(
          "p",
          {},
          `${p.entity || p.id} · ${p.attribute || ""}：${p.value || ""} ${p.unit || ""} · ${p.conditions || "未限定条件"} · ${p.current ? "前提有效" : p.reason}`,
        ),
      );
    if (!origin.current)
      nodes.push(h("p", { class: "error" }, origin.reasons.join("；")));
    nodes.push(
      h(
        "p",
        { class: "fine" },
        "推导结论不单独编辑。改变结论或前提时请创建新推导，并重新审批。",
      ),
    );
  }
  return h("section", { class: "check-detail" }, ...nodes);
}
async function inspectEvidence(r) {
  const base = libPath();
  const e = await api(
    base +
      `/sources/${r.source_id}/inspect?` +
      new URLSearchParams({ chunk_id: r.chunk_id }),
  );
  dialog(
    "原文件与解析结构",
    h("p", {}, e.source_name),
    h(
      "p",
      { class: "fine" },
      `原件 SHA256：${e.file_sha256} · 快照 SHA256：${e.chunks_sha256}`,
    ),
    h(
      "a",
      { href: "/api/" + base + `/sources/${r.source_id}/original` },
      "下载原文件核对页面／图表",
    ),
    ...e.warnings.map((w) => h("p", { class: "error" }, w)),
    h("h3", {}, "RAGFlow / 来源解析文字"),
    h("pre", {}, e.parsed_text),
    h("h3", {}, "原文件原生结构（不含图片理解）"),
    h("pre", {}, JSON.stringify(e.native, null, 2)),
    h("button", { onclick: () => reviewFact(r) }, "返回事实审核"),
  );
}
async function renderChecks() {
  const jobs = await api(libPath() + "/jobs");
  const active = jobs.some((j) => ["queued", "running"].includes(j.status));
  state.jobActive = active;
  const actions = h(
    "div",
    { class: "toolbar" },
    hintedButton(
      "本地结构检查",
      "使用本地规则检查主体归属、单位、来源完整性与可能冲突，不调用模型，无 API 费用。结果仅供审核参考。",
      {
        ...(active ? { disabled: "" } : {}),
        onclick: () => startJob("check-local"),
      },
    ),
    hintedButton(
      "模型语义检查",
      "调用当前配置的模型，对照原文、原文件结构和已确认事实核验候选，可能产生 API 费用。只提交检查意见，不自动确认或发布。",
      {
        class: "primary",
        ...(active ? { disabled: "" } : {}),
        onclick: () => startJob("check"),
      },
    ),
    hintedButton(
      "创建推导候选",
      "以当前有效、已确认的事实为前提，记录推理过程并生成待审核结论。前提失效后，依赖它的结论会停止提供。",
      { onclick: createDerivation },
    ),
  );
  const guide = h(
    "details",
    { class: "card workflow-guide" },
    h("summary", {}, "来源 → 提取 → 检查 → 你审批 → 发布"),
    h(
      "p",
      {},
      "同步或提取后自动做本地结构检查；模型检查由你明确启动。你也可以让持检查 MCP Token 的智能体，回看原文件并提交核验意见。",
    ),
    h(
      "p",
      {},
      "支持、疑点、冲突和证据不足均是检查意见。已审批事实是带条件的前提，不能据此简单给待定事实判真假。",
    ),
    h(
      "p",
      {},
      "推导候选记录前提和推理。前提被淘汰、来源更新、超出有效期或授权范围时，推导结论停止提供，需重新推导和审批。",
    ),
    h(
      "button",
      {
        onclick: async () => {
          state.tab = "facts";
          state.status = "pending";
          state.offset = 0;
          await refreshContent();
        },
      },
      "回到待审核事实",
    ),
  );
  const task = h(
    "div",
    { class: "card" },
    h("h3", {}, "任务进度"),
    ...jobs
      .slice(0, 8)
      .map((j) =>
        h(
          "div",
          { class: "job" },
          `${jobNames[j.kind] || j.kind} · ${{ queued: "排队中", running: "运行中", succeeded: "完成", failed: "失败" }[j.status]} · ${j.progress}/${j.total}`,
          j.result ? jobResult(j.result) : null,
          j.error ? h("p", { class: "error" }, j.error) : null,
        ),
      ),
  );
  $("#content").replaceChildren(actions, guide, task);
}
async function createDerivation() {
  const facts = await api(libPath() + "/approved-premises");
  if (!facts.length) {
    dialog(
      "创建推导候选",
      h(
        "p",
        {},
        "当前没有有效的已审批前提。请先审核事实；推导不能以待定事实为已确立前提。",
      ),
    );
    return;
  }
  const selected = new Set();
  const search = h("input", {
    type: "search",
    placeholder: "筛选前提：主体、属性或值",
  });
  const list = h("div", { class: "premise-list" });
  const render = () =>
    list.replaceChildren(
      ...facts
        .filter((f) =>
          [f.entity, f.attribute, f.value].some((v) =>
            v.includes(search.value),
          ),
        )
        .map((f) =>
          h(
            "label",
            { class: "checklabel" },
            h("input", {
              type: "checkbox",
              checked: selected.has(f.id),
              onchange: (e) =>
                e.target.checked ? selected.add(f.id) : selected.delete(f.id),
            }),
            `${f.entity} · ${f.attribute}：${f.value} ${f.unit} · ${f.conditions || "未限定条件"}`,
          ),
        ),
    );
  search.addEventListener("input", render);
  render();
  const rule = h(
    "select",
    { "aria-label": "推导方式" },
    h("option", { value: "reasoned" }, "一般推理建议（需人工核验）"),
    h(
      "option",
      { value: "ratio_complement" },
      "严格比例计算：补比例 = 1 - 原比例",
    ),
  );
  const controls = {};
  const nodes = [];
  for (const f of fields) {
    const [label, input] = inputField(
      f,
      fieldNames[f],
      "",
      ["value", "conditions"].includes(f) ? "textarea" : "input",
      f.startsWith("valid_") ? { type: "date" } : {},
    );
    controls[f] = input;
    nodes.push(label);
  }
  rule.addEventListener("change", () => {
    for (const c of Object.values(controls))
      c.disabled = rule.value !== "reasoned";
  });
  const [reasonLabel, reason] = inputField(
    "reasoning",
    "推理过程及适用边界",
    "",
    "textarea",
  );
  const button = h(
    "button",
    {
      class: "primary",
      onclick: () =>
        busy(button, async () => {
          const ids = [...selected];
          const revisions = Object.fromEntries(
            facts
              .filter((f) => selected.has(f.id))
              .map((f) => [f.id, f.revision]),
          );
          await api(libPath() + "/derivations", "POST", {
            premise_ids: ids,
            premise_revisions: revisions,
            rule: rule.value,
            reasoning: reason.value,
            candidate: Object.fromEntries(
              fields.map((f) => [f, controls[f].value]),
            ),
          });
          closeDialog();
          state.tab = "facts";
          state.status = "pending";
          state.offset = 0;
          await refreshLibraries();
        }),
    },
    "保存待审核推导",
  );
  dialog(
    "创建推导候选",
    h(
      "p",
      { class: "fine" },
      "选择1–16条已审批前提。结论不会自动确认或发布。严格规则只适用于同一对象、基线和条件的无修饰比例。",
    ),
    search,
    list,
    h("label", {}, "推导方式", rule),
    ...nodes,
    reasonLabel,
    button,
  );
}

initializeSidebar();
$("#sidebar-toggle").addEventListener("click", () =>
  setSidebarCollapsed(
    !$("#workspace").classList.contains("sidebar-collapsed"),
    true,
  ),
);
window.addEventListener("resize", hideHint);
window.addEventListener("scroll", hideHint, true);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") hideHint();
});

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#login-error").textContent = "";
  try {
    const response = await api("login", "POST", {
      token: $("#login-token").value,
    });
    $("#login-token").value = "";
    state.csrf = response.csrf;
    await initialize();
  } catch (error) {
    $("#login-error").textContent = error.message;
  }
});
$("#logout").addEventListener("click", async () => {
  await api("logout", "POST", {});
  closeDialog();
  showLogin();
});
$("#create-library").addEventListener("click", createLibrary);
$("#refresh").addEventListener("click", () =>
  refreshLibraries().catch((e) => notice(e.message, true)),
);
$("#dialog-close").addEventListener("click", closeDialog);
$("#dialog").addEventListener("close", () =>
  $("#dialog-body").replaceChildren(),
);
setInterval(async () => {
  if (
    !state.csrf ||
    !state.library ||
    !["sources", "checks"].includes(state.tab) ||
    $("#dialog").open
  )
    return;
  try {
    const jobs = await api(libPath() + "/jobs");
    if (
      state.jobActive ||
      jobs.some((j) => ["queued", "running"].includes(j.status))
    )
      await refreshLibraries();
  } catch (error) {
    notice(error.message, true);
  }
}, 5000);
api("session")
  .then((data) => {
    state.csrf = data.csrf;
    return initialize();
  })
  .catch(() => showLogin());

window.addEventListener("unhandledrejection", (event) => {
  notice(event.reason?.message || "请求失败，请重试。", true);
  event.preventDefault();
});
