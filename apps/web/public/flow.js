import { $, $$, api, apiMessage, button, dateLabel, element, flows, icon, money, svg } from "./ui.js";
import { workspaceId } from "./workspaces.js";

function treeFrom(data) {
  const nodes = new Map();
  const roots = Object.entries(flows).map(([kind, style]) => ({
    key: kind, kind, name: style.root, amount: data.summary[kind], count: data.flow_counts[kind],
    color: style.color, icon: style.icon, type: "root", children: [],
  }));
  roots.forEach(node => nodes.set(node.key, node));
  for (const category of data.categories) {
    if (!category.count) continue;
    const node = { ...category, key: "c" + category.id, type: "category", icon: "tags",
      parent: category.parent_id ? "c" + category.parent_id : category.kind, children: [] };
    nodes.set(node.key, node);
  }
  for (const node of nodes.values()) {
    if (node.parent) nodes.get(node.parent).children.push(node);
  }
  for (const root of roots) {
    const unassigned = data.unassigned[root.kind];
    if (unassigned.count) {
      const node = {
        ...unassigned, name: "Uncategorized", kind: root.kind, key: "u" + root.kind, type: "unassigned",
        parent: root.key, icon: "tags", color: root.color, direct_count: unassigned.count, children: [],
      };
      root.children.push(node);
      nodes.set(node.key, node);
    }
  }
  for (const node of nodes.values()) node.children.sort((a, b) => Number(BigInt(b.amount) - BigInt(a.amount)));
  return { roots, nodes };
}

export function moneyFlow(actions) {
  const panel = $("#flow-panel");
  const scroll = $("#flow-map");
  const canvas = $("#flow-canvas");
  const inspector = $("#flow-inspector");
  const filters = $("#flow-filters");
  const batch = 8;
  const nodeHeight = 47;
  const rowHeight = 57;
  const reset = $("#flow-reset");
  const fullscreen = $("#flow-fullscreen");
  let snapshot, period, tree, scope;
  let filter = "all";
  let selected = null;
  let expanded = new Set();
  const branches = new Map();

  function remember() {
    try { localStorage.setItem(scope, JSON.stringify({ filter, selected, expanded: [...expanded] })); }
    catch { /* Expansion stays available during this session. */ }
  }

  function restore(key) {
    scope = key;
    filter = "all";
    selected = null;
    expanded = new Set();
    try {
      const state = JSON.parse(localStorage.getItem(key));
      if (Object.hasOwn(flows, state?.filter)) filter = state.filter;
      if (typeof state?.selected === "string") selected = state.selected;
      if (Array.isArray(state?.expanded)) expanded = new Set(state.expanded.filter(value => typeof value === "string"));
    } catch { /* A fresh view still works if browser storage is unavailable. */ }
  }

  function clearRequests() {
    for (const state of branches.values()) state.request?.abort();
    branches.clear();
  }

  function setFilter(next) {
    filter = next;
    selected = next === "all" ? null : next;
    remember();
    scroll.scrollTo(0, 0);
    draw();
    loadVisibleBranches();
  }

  function nodeFor(key) {
    return tree.nodes.get(key) ?? [...branches.values()].flatMap(state => state.nodes).find(node => node.key === key);
  }

  function categoryFilter(node) {
    return node.type === "unassigned" ? "uncategorized" : node.id;
  }

  async function loadEntries(node) {
    if (!node?.direct_count) return;
    const state = branches.get(node.key) ?? { nodes: [], page: 0 };
    if (state.request) return;
    branches.set(node.key, state);
    const request = new AbortController();
    state.request = request;
    state.error = "";
    if (panel.clientWidth) renderInspector();
    const query = new URLSearchParams({
      period, kind: node.kind, category: categoryFilter(node), page: state.page + 1, size: batch,
      sample: snapshot.sample ? "1" : "0",
    });
    try {
      const data = await api("/api/transactions?" + query, { signal: request.signal });
      if (request.signal.aborted) return;
      state.nodes.push(...data.entries.map(entry => ({
        key: "e" + entry.id, kind: node.kind, name: entry.name, type: "entry", icon: "transactions",
        parent: node.key, amount: entry.amount, color: node.color, children: [], entry,
      })));
      state.page += 1;
    } catch (error) {
      if (!request.signal.aborted) state.error = apiMessage(error);
    } finally {
      if (!request.signal.aborted) {
        state.request = null;
        draw();
      }
    }
  }

  function loadVisibleBranches() {
    if (!tree || !panel.clientWidth) return;
    for (const key of expanded) {
      const node = tree.nodes.get(key);
      if (!node?.direct_count || branches.has(key) || (filter !== "all" && node.kind !== filter)) continue;
      let ancestor = tree.nodes.get(node.parent);
      while (ancestor?.parent && expanded.has(ancestor.key)) ancestor = tree.nodes.get(ancestor.parent);
      if (ancestor?.type === "root") loadEntries(node);
    }
  }

  function select(node) {
    if (node.type === "root") {
      setFilter(filter === node.kind ? "all" : node.kind);
    } else {
      selected = node.key;
      if (node.type !== "entry") {
        if (expanded.has(node.key)) expanded.delete(node.key);
        else expanded.add(node.key);
      }
      remember();
    }
    draw(node.key);
    loadVisibleBranches();
  }

  function renderInspector() {
    const context = nodeFor(selected);
    if (!context) {
      const hasFlows = tree.roots.some(root => root.count);
      inspector.replaceChildren(element("p", "graph-caption", hasFlows
        ? "Line width represents the amount. Select a node to open its branch."
        : "No income, expenses or investment contributions in this period."));
      if (!hasFlows) {
        const add = button("Add transaction", "text-button", "plus");
        add.addEventListener("click", () => actions.addEntry("expense", null));
        inspector.append(add);
      }
      return;
    }
    const details = element("div");
    details.append(element("strong", "", context.name), " · ", money(context.amount));
    if (context.entry) {
      details.append(" · ", dateLabel(context.entry.posted_on));
      if (context.entry.labels.length) details.append(" · ", context.entry.labels.join(", "));
    } else if (context.count) {
      details.append(" · ", context.count + (context.count === 1 ? " transaction" : " transactions"));
    }

    const controls = element("div", "flow-inspector-actions");
    const addControl = (label, action, name = "") => {
      const handle = button(label, "text-button", name);
      handle.addEventListener("click", action);
      controls.append(handle);
    };
    if (context.entry) {
      if (!snapshot.sample) addControl(context.entry.shared ? "Open shared payments" : "Edit transaction",
        () => actions.editEntry(context.entry), context.entry.shared ? "arrow-up-right" : "edit");
    } else {
      if (context.direct_count || (context.type === "root" && context.count)) {
        addControl(context.type === "category" && context.children.length ? "View direct transactions" : "View transactions",
          () => actions.viewEntries({ kind: context.kind, category: categoryFilter(context), period }));
      }
      const branch = branches.get(context.key);
      if (expanded.has(context.key) && (branch?.nodes.length ?? 0) < (context.direct_count ?? 0)) {
        if (branch?.request) controls.append(element("span", "", "Loading transactions…"));
        else addControl(branch?.error ? "Retry" : "Show more nodes", () => loadEntries(context));
      }
      if (!snapshot.sample) {
        addControl("Add transaction", () => actions.addEntry(context.kind, context.id), "plus");
        if (context.type === "category" && !context.profile_id) addControl("Add subcategory", () => actions.addCategory(context.kind, context.id), "plus");
      }
    }
    inspector.replaceChildren(details, controls);
    const error = branches.get(context.key)?.error;
    if (error) inspector.append(element("p", "notice", error));
  }

  function draw(focusKey = null) {
    if (!tree || !panel.clientWidth) return;
    const lastFocusedKey = focusKey ?? document.activeElement?.dataset.node;
    const visibleRoots = tree.roots.filter(root => filter === "all" || root.kind === filter);
    const expandedKeys = expanded;
    const items = [];
    const edges = [];
    let lastDepth = 0;
    let cursor = 42;

    function place(node, depth) {
      lastDepth = Math.max(lastDepth, depth);
      const children = node.type === "root" || expandedKeys.has(node.key) ? [...node.children] : [];
      if (expandedKeys.has(node.key)) children.push(...(branches.get(node.key)?.nodes ?? []));
      const childPositions = children.map(child => place(child, depth + 1));
      let y;
      if (childPositions.length) {
        y = (childPositions[0].y + childPositions.at(-1).y) / 2;
      } else {
        y = cursor;
        cursor += rowHeight;
      }
      const position = { node, y, depth };
      items.push(position);
      for (const child of childPositions) edges.push({ from: position, to: child });
      return position;
    }

    for (const root of visibleRoots) {
      place(root, 0);
      cursor += 10;
    }
    const columns = lastDepth + 1;
    const compact = scroll.clientWidth < 560;
    const nodeWidth = compact ? 148 : 174;
    const maxGap = columns < 3 ? 240 : 125;
    const gap = Math.min(maxGap, Math.max(compact ? 38 : 63,
      (scroll.clientWidth - columns * nodeWidth - 52) / (Math.max(1, columns - 1) * 1.4)));
    const used = columns * nodeWidth + (columns - 1) * gap;
    const width = Math.max(used + (compact ? 32 : 55), scroll.clientWidth);
    const height = Math.max(140, cursor - 14);
    const xFor = depth => (width - used) / 2 + depth * (nodeWidth + gap);
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    const diagram = svg("svg", { class: "flow-edges", width, height, "aria-hidden": "true" });
    const size = Math.max(1, ...visibleRoots.map(root => Number(root.amount)));
    for (const { from, to } of edges) {
      const x1 = xFor(from.depth) + nodeWidth;
      const x2 = xFor(to.depth);
      const bend = (x1 + x2) / 2;
      const className = "flow-edge " + to.node.color;
      diagram.append(svg("path", {
        d: "M" + x1 + " " + from.y + " C" + bend + " " + from.y + " " + bend + " " + to.y + " " + x2 + " " + to.y,
        class: className, "stroke-width": 1.15 + 12 * Number(to.node.amount) / size,
      }));
    }

    const controls = new Map();
    for (const { node, depth, y } of items.sort((a, b) => a.depth - b.depth || a.y - b.y)) {
      const control = button("", "flow-node " + node.type + " " + node.color);
      control.dataset.node = node.key;
      control.title = node.name + " · " + money(node.amount);
      control.setAttribute("aria-pressed", node.key === selected);
      if (node.type === "category" || node.type === "unassigned") {
        control.setAttribute("aria-expanded", expandedKeys.has(node.key));
      }
      control.style.left = xFor(depth) + "px";
      control.style.top = (y - nodeHeight / 2) + "px";
      control.style.width = nodeWidth + "px";
      const copy = element("span", "node-title");
      copy.append(element("strong", "", node.name), element("small", "node-amount", money(node.amount)));
      control.append(element("span", "row-icon", icon(node.icon)), copy);
      if (node.type !== "entry" && (node.children.length || node.direct_count)) {
        control.append(icon(expandedKeys.has(node.key) ? "chevron-down" : "chevron-right"));
      }
      control.addEventListener("click", () => select(node));
      controls.set(node.key, control);
    }
    canvas.replaceChildren(diagram, ...controls.values());
    for (const control of $$("button", filters)) control.setAttribute("aria-pressed", control.dataset.flow === filter);
    renderInspector();
    if (lastFocusedKey) controls.get(lastFocusedKey)?.focus({ preventScroll: true });
  }

  filters.addEventListener("click", event => {
    const control = event.target.closest("[data-flow]");
    if (control) setFilter(control.dataset.flow);
  });
  reset.addEventListener("click", () => {
    clearRequests();
    expanded.clear();
    setFilter("all");
  });

  function updateFullscreen() {
    const active = document.fullscreenElement === panel || panel.classList.contains("flow-overlay");
    fullscreen.textContent = active ? "Exit fullscreen" : "Fullscreen";
    fullscreen.setAttribute("aria-pressed", String(active));
    document.body.classList.toggle("flow-overlay-open", panel.classList.contains("flow-overlay"));
    draw();
  }

  async function toggleFullscreen() {
    if (document.fullscreenElement === panel) await document.exitFullscreen();
    else if (panel.classList.contains("flow-overlay")) panel.classList.remove("flow-overlay");
    else {
      try {
        if (!panel.requestFullscreen) throw new Error("Fullscreen unavailable");
        await panel.requestFullscreen();
      } catch { panel.classList.add("flow-overlay"); }
    }
    updateFullscreen();
  }

  function exitFullscreen() {
    if (document.fullscreenElement === panel) document.exitFullscreen().catch(() => {});
    panel.classList.remove("flow-overlay");
    updateFullscreen();
  }

  fullscreen.addEventListener("click", toggleFullscreen);
  document.addEventListener("fullscreenchange", updateFullscreen);
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && panel.classList.contains("flow-overlay")) exitFullscreen();
  });
  new ResizeObserver(() => draw()).observe(scroll);

  return {
    draw() { draw(); loadVisibleBranches(); },
    exitFullscreen,
    update(data, horizon) {
      clearRequests();
      const key = "quant-flow-" + workspaceId() + "-" + (data.sample ? "sample" : "real");
      if (key !== scope) restore(key);
      snapshot = data;
      period = horizon;
      tree = treeFrom(data);
      draw();
      loadVisibleBranches();
    },
  };
}
