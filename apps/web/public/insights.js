import { $, api, apiMessage, button, dateLabel, element, flows, icon, money, options, svg } from "./ui.js";

const monthTitle = value => dateLabel(value + "-01", { month: "long", year: "numeric" });
const ordinal = value => Number(value.slice(0, 4)) * 12 + Number(value.slice(5)) - 1;
const monthAt = value => String(Math.floor(value / 12)).padStart(4, "0") + "-" + String(value % 12 + 1).padStart(2, "0");
const sign = value => (BigInt(value) > 0n ? "+" : "") + money(value, { fixed: true });
const magnitude = value => value < 0n ? -value : value;

function heading(root, title, subtitle, controls) {
  const page = element("div", "page-heading");
  const copy = element("div");
  copy.append(element("h1", "", title), element("p", "page-description", subtitle));
  page.append(copy, controls);
  root.append(page);
}

function selectField(label, input) {
  const field = element("label", "field");
  field.append(element("span", "", label), input);
  return field;
}

function feedback(region, message, error = false) {
  region.replaceChildren(element("p", error ? "notice" : "muted", message));
}

function query(snapshot, month, additional = {}) {
  return new URLSearchParams({ month, sample: snapshot.sample ? "1" : "0", ...additional });
}

function sideNodes(data) {
  const nodes = new Map();
  for (const [kind, style] of Object.entries(flows)) {
    nodes.set(kind, { key: kind, kind, name: style.root, color: style.color, amount: data.summary[kind], parent: null });
    const value = data.unassigned[kind];
    if (value.count) nodes.set("u" + kind, {
      key: "u" + kind, parent: kind, category: "uncategorized", kind,
      name: "Uncategorized", color: style.color, amount: value.amount,
    });
  }
  for (const category of data.categories) {
    if (!category.count) continue;
    const key = "c" + category.id;
    nodes.set(key, { ...category, key, category: category.id, parent: category.parent_id ? "c" + category.parent_id : category.kind });
  }
  return nodes;
}

function replayGraph() {
  const viewport = element("div", "replay-viewport");
  const canvas = element("div", "replay-canvas");
  const lines = svg("svg", { "aria-hidden": "true", preserveAspectRatio: "none" });
  const marks = new Map(), nodes = new Map();
  canvas.append(lines);
  viewport.append(canvas);
  const title = element("h3");
  const panel = element("article", "panel replay-panel");
  panel.append(title, viewport);

  function draw({ data, amounts, items, highlights, select }) {
    title.textContent = monthTitle(data.month) + " · through " + dateLabel(data.through, { day: "numeric", month: "short" });
    const height = Math.max(135, ...items.map(item => item.y + 39));
    canvas.style.height = height + "px";
    lines.setAttribute("viewBox", "0 0 100 " + height);
    const keys = new Set(items.map(item => item.node.key));
    const scale = Math.max(1, Number(data.scale));
    for (const [key, control] of nodes) {
      if (!keys.has(key)) {
        control.remove();
        nodes.delete(key);
      }
    }
    for (const [key, line] of marks) {
      if (!keys.has(key)) {
        line.remove();
        marks.delete(key);
      }
    }
    for (const { node, y, from } of items) {
      const amount = amounts.get(node.key)?.amount ?? "0";
      const proportion = Number(amount) / scale;
      const isChange = highlights.has(node.key);
      let control = nodes.get(node.key);
      if (!control) {
        control = button("", "replay-node " + node.color);
        const text = element("strong"), value = element("small", "mono");
        control.append(text, value);
        canvas.append(control);
        nodes.set(node.key, control);
      }
      control.style.top = y + "px";
      control.style.left = from === null ? "3%" : "52%";
      control.style.minHeight = (36 + 12 * proportion) + "px";
      control.classList.toggle("replay-highlight", isChange);
      control.classList.toggle("replay-branch", from !== null);
      control.classList.toggle("replay-zero", BigInt(amount) === 0n);
      control.children[0].textContent = node.name;
      control.children[1].textContent = money(amount, { fixed: true });
      control.onclick = () => select(node, data.month, amount);
      if (from !== null) {
        let path = marks.get(node.key);
        if (!path) {
          path = svg("path", { class: "replay-edge " + node.color, "vector-effect": "non-scaling-stroke" });
          lines.append(path);
          marks.set(node.key, path);
        }
        path.setAttribute("d", "M39 " + from + " C45 " + from + " 45 " + y + " 52 " + y);
        path.style.strokeWidth = (BigInt(amount) === 0n ? 0 : 1.2 + 14 * proportion) + "px";
        path.classList.toggle("replay-line-highlight", isChange);
      }
    }
  }
  return { panel, draw };
}

export function setupReplay(root, { viewEntries }) {
  let snapshot, selected, pinned, branch = null, parentBranch = null, cache = new Map(), request, timer;
  const back = element("a", "secondary", "Live money flow");
  back.href = "#flow";
  heading(root, "Money flow replay", "Compare the income, expenses and investments recorded in each month.", back);
  const controls = element("div", "replay-controls panel");
  const month = element("input");
  month.type = "month";
  const slider = element("input");
  slider.type = "range";
  slider.step = "1";
  const kind = element("select");
  options(kind, [["all", "All money flows"], ["income", "Income"], ["expense", "Expenses"], ["investment", "Investments"]], "all");
  const pin = button("Pin selected month", "secondary");
  const unpin = button("Unpin comparison", "text-button");
  const up = button("Show parent branch", "text-button");
  controls.append(selectField("Month", month), selectField("Scrub by month", slider), selectField("Money flow", kind), pin, unpin, up);
  const state = element("div", "analysis-status");
  state.setAttribute("role", "status");
  const graphs = element("div", "replay-graphs");
  const first = replayGraph(), second = replayGraph();
  graphs.append(first.panel, second.panel);
  const detail = element("div", "replay-detail panel");
  const compare = element("div", "replay-detail panel");
  root.append(controls, state, graphs, detail, compare);
  first.panel.hidden = true;
  second.panel.hidden = true;
  detail.hidden = true;
  compare.hidden = true;
  unpin.hidden = true;
  up.hidden = true;

  function setMonth(value) {
    if (!value || value > month.max || value < month.min) return;
    selected = value;
    month.value = value;
    slider.value = String(ordinal(value));
    clearTimeout(timer);
    timer = setTimeout(load, 65);
  }

  async function dataFor(value, signal) {
    if (cache.has(value)) return cache.get(value);
    const data = await api("/api/insights/replay?" + query(snapshot, value), { signal });
    if (!signal.aborted) cache.set(value, data);
    return data;
  }

  function inspect(node, value, amount, union) {
    const show = button("View transactions", "text-button", "arrow-right");
    show.addEventListener("click", () => viewEntries({
      kind: node.kind, category: node.category, month: value, descendants: Number.isInteger(node.category),
    }));
    detail.replaceChildren(element("strong", "", node.name), element("span", "muted", monthTitle(value) + " · " + money(amount, { fixed: true })), show);
    detail.hidden = false;
    if (node.parent && [...union.values()].some(item => item.parent === node.key)) {
      branch = node.key;
      load();
    }
  }

  async function load() {
    if (!snapshot || root.hidden) return;
    request?.abort();
    const controller = new AbortController();
    request = controller;
    const value = selected ?? snapshot.today.slice(0, 7);
    try {
      const [current, previous] = await Promise.all([
        dataFor(value, controller.signal),
        pinned ? dataFor(pinned, controller.signal) : null,
      ]);
      if (controller.signal.aborted) return;
      const oldest = current.first < value ? current.first : value;
      month.min = oldest;
      month.max = snapshot.today.slice(0, 7);
      month.value = value;
      slider.min = String(ordinal(oldest));
      slider.max = String(ordinal(month.max));
      slider.value = String(ordinal(value));
      slider.disabled = slider.min === slider.max;
      slider.setAttribute("aria-valuetext", monthTitle(value));
      unpin.hidden = !pinned;
      pin.disabled = value === pinned;
      graphs.classList.toggle("replay-pair", Boolean(previous));
      const currentNodes = sideNodes(current);
      const previousNodes = previous ? sideNodes(previous) : new Map();
      const union = new Map([...previousNodes, ...currentNodes]);
      if (branch && !union.has(branch)) branch = null;
      parentBranch = union.get(branch)?.parent;
      up.hidden = !branch;
      const roots = branch ? [union.get(branch)] : [...union.values()].filter(node =>
        !node.parent && (kind.value === "all" || kind.value === node.kind));
      const items = [];
      let y = 36;
      for (const node of roots) {
        const children = [...union.values()].filter(item => item.parent === node.key)
          .sort((a, b) => a.name.localeCompare(b.name));
        const center = y + (Math.max(1, children.length) - 1) * 29;
        items.push({ node, y: center, from: null });
        for (const [index, child] of children.entries()) items.push({ node: child, y: y + 58 * index, from: center });
        y += 58 * Math.max(1, children.length) + 12;
      }
      const changes = previous ? items.filter(item => item.from !== null).map(({ node }) => ({
        node, difference: BigInt(currentNodes.get(node.key)?.amount ?? 0) - BigInt(previousNodes.get(node.key)?.amount ?? 0),
      })).filter(item => item.difference !== 0n)
        .sort((a, b) => Number(magnitude(b.difference) - magnitude(a.difference))).slice(0, 3) : [];
      const highlights = new Set(changes.map(change => change.node.key));
      const select = (node, monthValue, amount) => inspect(node, monthValue, amount, union);
      first.panel.hidden = false;
      second.panel.hidden = !previous;
      first.draw({ data: previous ?? current, amounts: previous ? previousNodes : currentNodes, items, highlights, select });
      if (previous) second.draw({ data: current, amounts: currentNodes, items, highlights, select });
      compare.hidden = !previous;
      if (previous) {
        const differences = changes.map(({ node, difference }) => node.name + " " + sign(difference));
        compare.replaceChildren(element("strong", "", "Largest branch changes"),
          element("p", "muted", differences.join(" · ") || "No change in the visible branches."));
      }
      state.replaceChildren();
    } catch (error) {
      if (!controller.signal.aborted) feedback(state, apiMessage(error), true);
    }
  }

  month.addEventListener("change", () => { detail.hidden = true; setMonth(month.value); });
  slider.addEventListener("input", () => { detail.hidden = true; setMonth(monthAt(Number(slider.value))); });
  kind.addEventListener("change", () => { branch = null; detail.hidden = true; load(); });
  pin.addEventListener("click", () => { pinned = selected ?? snapshot.today.slice(0, 7); load(); });
  unpin.addEventListener("click", () => { pinned = null; load(); });
  up.addEventListener("click", () => {
    branch = parentBranch?.startsWith("c") ? parentBranch : null;
    detail.hidden = true;
    load();
  });

  return {
    open() { if (snapshot) load(); else feedback(state, "Loading recorded months…"); },
    update(value) {
      if (snapshot?.sample !== value.sample) {
        selected = pinned = branch = null;
        detail.hidden = true;
      }
      snapshot = value;
      cache = new Map();
      if (!root.hidden) load();
    },
  };
}

function evidenceTable(rows, show) {
  const table = element("table");
  const head = element("tr");
  for (const name of ["Date", "Entry", "Amount"]) head.append(element("th", "", name));
  table.append(element("thead", "", head));
  const body = element("tbody");
  for (const row of rows) {
    const tr = element("tr");
    tr.append(element("td", "", dateLabel(row.posted_on)), element("td", "", row.name),
      element("td", "mono ledger-amount", money(row.amount, { fixed: true })));
    if (row.note) tr.children[1].title = row.note;
    body.append(tr);
  }
  table.append(body);
  const wrapper = element("div", "table-scroll", table);
  show.replaceChildren(wrapper);
}

function dataInspector(panel, name, endpoint, snapshot, month, parameters, prior = false) {
  panel.hidden = false;
  const title = element("h3", "", name);
  const content = element("div");
  const controls = element("div", "analysis-entry-actions");
  const current = button("Triggering entries", "secondary");
  const history = button("Historical baseline", "secondary");
  if (prior) controls.append(current, history);
  panel.replaceChildren(title, controls, content);
  let request;
  async function show(usePrior, page = 1) {
    request?.abort();
    const controller = new AbortController();
    request = controller;
    current.setAttribute("aria-pressed", String(!usePrior));
    history.setAttribute("aria-pressed", String(usePrior));
    feedback(content, "Loading entries…");
    try {
      const data = await api(endpoint + "?" + query(snapshot, month, { ...parameters, page, prior: usePrior ? "1" : "0" }), {
        signal: controller.signal,
      });
      if (controller.signal.aborted || !panel.contains(content)) return;
      if (data.rows.length) evidenceTable(data.rows, content);
      else feedback(content, "No entries for this selection.");
      if (data.total > data.size) {
        const pages = element("div", "pagination");
        const older = button("Previous", "secondary"), later = button("Next", "secondary");
        older.disabled = data.page <= 1;
        later.disabled = data.page * data.size >= data.total;
        older.addEventListener("click", () => show(usePrior, page - 1));
        later.addEventListener("click", () => show(usePrior, page + 1));
        pages.append(element("span", "", "Page " + data.page), older, later);
        content.append(pages);
      }
    } catch (error) {
      if (!controller.signal.aborted) feedback(content, apiMessage(error), true);
    }
  }
  current.addEventListener("click", () => show(false));
  history.addEventListener("click", () => show(true));
  show(false);
}

function waterfallGraph(steps, select) {
  const width = 720, height = 229;
  const start = 65, right = 14, top = 18, bottom = 199;
  const values = steps.flatMap(step => [Number(step.from), Number(step.to)]);
  const low = Math.min(0, ...values), high = Math.max(100, ...values);
  const y = quantity => bottom - (Number(quantity) - low) * (bottom - top) / (high - low);
  const slot = (width - start - right) / steps.length;
  const chart = svg("svg", { viewBox: "0 0 " + width + " " + height, role: "img",
    "aria-label": "Waterfall showing changes in net worth from opening to closing balance. Use the buttons below to see entries." });
  const ticks = Array.from({ length: 5 }, (_, index) => low + (high - low) * index / 4);
  for (const tick of ticks) {
    const level = y(tick);
    chart.append(svg("path", { d: "M" + start + " " + level + " H" + (width - right), class: "waterfall-grid" }));
    const label = svg("text", { x: start - 8, y: level, "text-anchor": "end", "dominant-baseline": "middle" });
    label.textContent = new Intl.NumberFormat("en", { style: "currency", currency: "EUR", maximumFractionDigits: 1, notation: "compact" }).format(tick / 100);
    chart.append(label);
  }
  const labels = { start: "Opening", income: "Income", expense: "Expenses", valuation: "Value change",
    openings: "Accounts added", end: "Closing" };
  const keys = element("div", "waterfall-keys");
  steps.forEach((step, index) => {
    const center = start + slot * (index + .5);
    const finish = y(step.to);
    const begin = y(step.from);
    const barWidth = slot * .59;
    const total = step.key === "start" || step.key === "end";
    if (index < steps.length - 1) chart.append(svg("path", { d: "M" + (center + barWidth / 2) + " " + finish
      + " H" + (center + slot - barWidth / 2), class: "waterfall-connector" }));
    const rect = svg("rect", { x: center - barWidth / 2, y: Math.min(finish, begin), width: barWidth,
      height: Math.max(2, Math.abs(begin - finish)), rx: 4, class: "waterfall-bar " + (total ? "waterfall-total"
        : BigInt(step.value) < 0n ? "waterfall-negative" : "waterfall-positive") });
    const numeric = total ? money(step.value, { fixed: true }) : sign(step.value);
    const tip = svg("title");
    tip.textContent = step.label + " " + numeric;
    rect.append(tip);
    if (!total) rect.addEventListener("click", () => select(step));
    chart.append(rect);
    const label = svg("text", { x: center, y: bottom + 21, "text-anchor": "middle" });
    label.textContent = labels[step.key];
    chart.append(label);
    const key = total ? element("span", "waterfall-total-key") : button("", "secondary");
    key.append(element("span", "", labels[step.key]), element("strong", "mono", numeric));
    if (!total) key.addEventListener("click", () => select(step));
    keys.append(key);
  });
  const chartWrap = element("div", "waterfall-scroll", chart);
  const region = element("div");
  region.append(chartWrap, keys);
  return region;
}

export function setupInsights(root) {
  let snapshot, request;
  const month = element("input");
  month.type = "month";
  heading(root, "Financial insights", "Trace changes in net worth and find unusual spending.", selectField("Month", month));
  const summary = element("div", "panel analysis-panel");
  const title = element("div", "panel-heading");
  const copy = element("div");
  copy.append(element("h2", "", "What changed?"), element("p", "muted", "Changes in tracked net worth"));
  title.append(copy);
  const chart = element("div");
  const steps = element("section", "analysis-evidence");
  steps.hidden = true;
  summary.append(title, chart, steps);
  const detector = element("section", "panel analysis-panel");
  const detectorHeader = element("div", "panel-heading");
  const detectorCopy = element("div");
  detectorCopy.append(element("h2", "", "Spending detective"),
    element("p", "muted", "Recurring payments, price increases and category outliers."));
  detectorHeader.append(detectorCopy);
  const flags = element("div", "detective-flags");
  const entries = element("section", "analysis-evidence");
  entries.hidden = true;
  detector.append(detectorHeader, flags, entries);
  root.append(summary, detector);

  async function load() {
    if (!snapshot || root.hidden) return;
    request?.abort();
    const controller = new AbortController();
    request = controller;
    const value = month.value || snapshot.today.slice(0, 7);
    month.value = value;
    month.max = snapshot.today.slice(0, 7);
    steps.hidden = entries.hidden = true;
    feedback(chart, "Loading net worth changes…");
    feedback(flags, "Checking recorded spending…");
    const params = query(snapshot, value);
    const results = await Promise.allSettled([
      api("/api/insights/waterfall?" + params, { signal: controller.signal }),
      api("/api/insights/detective?" + params, { signal: controller.signal }),
    ]);
    if (controller.signal.aborted) return;
    const [worth, spending] = results;
    if (worth.status === "fulfilled") chart.replaceChildren(waterfallGraph(worth.value.steps, step => {
      dataInspector(steps, step.label, "/api/insights/waterfall", snapshot, value, { step: step.key });
    }));
    else feedback(chart, apiMessage(worth.reason), true);
    if (spending.status === "rejected") {
      feedback(flags, apiMessage(spending.reason), true);
      return;
    }
    if (!spending.value.flags.length) {
      feedback(flags, "No patterns or unusual changes detected for this month.");
      return;
    }
    const labels = { new: "New recurring charge", price: "Price increase", category: "Unusually high category" };
    flags.replaceChildren(...spending.value.flags.map(flag => {
      const row = button("", "secondary detective-flag");
      const main = element("span", "detective-copy");
      const amount = element("span", "detective-amount");
      main.append(element("small", "muted", labels[flag.kind]), element("strong", "", flag.label), element("small", "muted", flag.detail));
      amount.append(element("strong", "mono", money(flag.amount, { fixed: true })));
      if (flag.baseline !== null) amount.append(element("small", "muted", "Baseline " + money(flag.baseline, { fixed: true })));
      row.append(main, amount, icon("chevron-right"));
      row.addEventListener("click", () => dataInspector(entries, flag.label, "/api/insights/detective",
        snapshot, value, { flag: flag.id }, Boolean(flag.history)));
      return row;
    }));
  }

  month.addEventListener("change", load);
  return {
    open() { if (snapshot) load(); else feedback(chart, "Loading ledger…"); },
    update(value) {
      snapshot = value;
      if (!root.hidden) load();
    },
  };
}
