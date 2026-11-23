import { $, $$, dateLabel, dateRange, element, money, svg } from "./ui.js";

const seriesByKey = {
  wealth: { key: "wealth", title: "Net worth", color: "#5546e7", group: "balance" },
  cash: { key: "cash", title: "Cash", color: "#3b8ec3", group: "balance" },
  savings: { key: "savings", title: "Savings", color: "#209878", group: "balance" },
  investments: { key: "investments", title: "Investment balance", color: "#9163cc", group: "balance" },
  shared_balance: { key: "shared_balance", title: "Net shared balance", color: "#bd7934", group: "balance" },
  income: { key: "income", title: "Income", color: "#22846b", group: "flow" },
  expense: { key: "expense", title: "Expenses", color: "#cb5577", group: "flow", direction: -1 },
  investment: { key: "investment", title: "Invested", color: "#a77234", group: "flow", direction: -1 },
};
const views = {
  wealth: ["wealth", "cash", "savings", "investments", "shared_balance"],
  flow: ["income", "expense", "investment"],
  combined: ["wealth", "cash", "savings", "investments", "shared_balance", "income", "expense", "investment"],
};
const axisMoney = new Intl.NumberFormat("en", { style: "currency", currency: "EUR", notation: "compact", maximumFractionDigits: 1 });

function valueOf(point, series) {
  const cents = point[series.key];
  if (cents == null) return null;
  return Number(cents) * (series.direction ?? 1);
}

function exact(point, series) {
  const amount = point[series.key];
  return amount == null ? null : BigInt(amount) * BigInt(series.direction ?? 1);
}

function tickValues(min, max) {
  const step = (max - min || 100) / 4;
  const power = 10 ** Math.floor(Math.log10(step));
  const tick = [1, 2, 2.5, 5, 10].find(value => value * power >= step) * power;
  const first = Math.floor(min / tick);
  const last = Math.max(first + 1, Math.ceil(max / tick));
  return Array.from({ length: last - first + 1 }, (_, index) => (first + index) * tick);
}

function drawLine(points, series, xAt, yAt, bottom, shade) {
  const coordinates = points.flatMap((point, index) => {
    const value = valueOf(point, series);
    return value === null ? [] : [[xAt(index), yAt(value)]];
  });
  const group = svg("g");
  if (!coordinates.length) return group;
  const path = coordinates.map((pair, index) => (index ? "L" : "M") + pair.join(" ")).join(" ");
  if (shade) {
    const firstX = coordinates[0][0];
    const lastX = coordinates.at(-1)[0];
    group.append(svg("path", { d: path + " L" + lastX + " " + bottom + " L" + firstX + " " + bottom + " Z", fill: "url(#wealth-fill)" }));
  }
  group.append(svg("path", {
    d: path, fill: "none", stroke: series.color, "stroke-width": series.key === "wealth" ? 2.65 : 2,
    "stroke-linejoin": "round", "stroke-linecap": "round",
  }));
  if (coordinates.length <= 12) {
    for (const [cx, cy] of coordinates) group.append(svg("circle", { cx, cy, r: series.key === "wealth" ? 3 : 2.4, fill: series.color }));
  }
  return group;
}

function barSegments(points, groups) {
  return points.map(point => groups.map(group => {
    let positive = 0;
    let negative = 0;
    const segments = [];
    for (const series of group) {
      const value = valueOf(point, series);
      if (!value) continue;
      const start = value > 0 ? positive : negative;
      const end = start + value;
      if (value > 0) positive = end;
      else negative = end;
      segments.push({ start, end, color: series.color });
    }
    return segments;
  }));
}

export function trendChart() {
  const chart = $("#trend-chart");
  const drawing = $("svg", chart);
  const tooltip = $("#trend-tooltip");
  const legend = $("#trend-legend");
  const chartData = $("#chart-data");
  const chartStyles = $("#chart-styles");
  const table = $("#trend-table");
  const tableToggle = $("#trend-table-toggle");
  let snapshot;
  let style = "bar";
  let visible = [];
  let hiddenSeries = new Set();
  let layout;
  let active = null;

  function updateLegend() {
    const keys = views[chartData.value].filter(key => key !== "shared_balance" || snapshot.shared_balance !== null);
    if (keys.every(key => hiddenSeries.has(key))) hiddenSeries.clear();
    const controls = keys.map(key => {
      const series = seriesByKey[key];
      const enabled = !hiddenSeries.has(key);
      const control = element("button", "legend-toggle");
      control.type = "button";
      control.setAttribute("aria-pressed", enabled);
      const swatch = element("span", "swatch");
      swatch.style.setProperty("--swatch", series.color);
      control.append(swatch, series.title);
      control.addEventListener("click", () => {
        if (!enabled) hiddenSeries.delete(key);
        else if (visible.length > 1) hiddenSeries.add(key);
        updateLegend();
        render();
        renderTable();
        const newControl = [...legend.children].find(item => item.textContent === series.title);
        newControl?.focus();
      });
      return control;
    });
    visible = keys.filter(key => !hiddenSeries.has(key)).map(key => seriesByKey[key]);
    legend.replaceChildren(...controls);
  }

  function description() {
    const unit = { day: "day", month: "month", span: "period" }[snapshot.frequency];
    const scope = { wealth: "End-of-period balances", flow: "Income, expenses and contributions per " + unit,
      combined: "Balances and activity per " + unit }[chartData.value];
    $("#trend-description").textContent = scope;
  }

  function renderTable() {
    if (!snapshot || table.hidden) return;
    const node = element("table");
    const header = element("tr");
    header.append(element("th", "", "Period"), ...visible.map(series => element("th", "", series.title)));
    const head = element("thead", "", header);
    const body = element("tbody");
    for (const point of snapshot.points) {
      const row = element("tr");
      row.append(element("th", "", dateRange(point.from, point.to)),
        ...visible.map(series => element("td", "mono", money(exact(point, series), { fixed: true }))));
      body.append(row);
    }
    node.append(head, body);
    table.replaceChildren(node);
  }

  function render() {
    if (!snapshot || !chart.clientWidth) return;
    const { points, frequency } = snapshot;
    const width = chart.clientWidth;
    const height = chart.clientHeight;
    const top = 13;
    const bottom = height - 29;
    const left = width < 360 ? 46 : 55;
    const right = width - 17;
    const plotWidth = Math.max(1, right - left);
    const bars = style === "bar" ? visible.map(series => [series])
      : style === "stacked" ? ["balance", "flow"].map(group =>
        visible.filter(series => series.group === group && series.key !== "wealth")).filter(group => group.length)
      : [];
    const lines = style === "line" ? visible : style === "stacked" ? visible.filter(series => series.key === "wealth") : [];
    const segments = barSegments(points, bars);
    let low = 0;
    let high = 0;
    points.forEach((point, index) => {
      const values = [...lines.map(series => valueOf(point, series) ?? 0),
        ...segments[index].flatMap(column => column.map(item => item.end))];
      low = Math.min(low, ...values);
      high = Math.max(high, ...values);
    });
    const ticks = tickValues(low, high);
    const minimum = ticks[0], maximum = ticks.at(-1);
    const step = style === "line" ? plotWidth / Math.max(1, points.length - 1) : plotWidth / points.length;
    const xAt = index => points.length === 1 ? left + plotWidth / 2
      : left + step * (index + (style === "line" ? 0 : .5));
    const yAt = value => bottom - (value - minimum) / (maximum - minimum) * (bottom - top);
    drawing.setAttribute("viewBox", "0 0 " + width + " " + height);
    drawing.replaceChildren();

    const gradient = svg("linearGradient", { id: "wealth-fill", x1: "0", y1: "0", x2: "0", y2: "1" });
    gradient.append(svg("stop", { offset: "0%", "stop-color": "#7565ed", "stop-opacity": ".17" }),
      svg("stop", { offset: "100%", "stop-color": "#7565ed", "stop-opacity": ".015" }));
    const definitions = svg("defs");
    definitions.append(gradient);
    drawing.append(definitions);

    for (const tick of ticks) {
      const y = yAt(tick);
      const label = svg("text", { x: left - 8, y, "text-anchor": "end", "dominant-baseline": "middle" });
      label.textContent = axisMoney.format(tick / 100);
      drawing.append(svg("line", { x1: left, x2: right, y1: y, y2: y, class: "grid-line" + (tick === 0 ? " zero-line" : "") }), label);
    }
    const count = Math.min(points.length, Math.max(2, Math.floor(plotWidth / 60) + 1));
    for (let index = 0; index < count; index += 1) {
      const pointIndex = count === 1 ? 0 : Math.round(index * (points.length - 1) / (count - 1));
      const point = points[pointIndex];
      const x = Math.max(25, Math.min(width - 26, xAt(pointIndex)));
      const label = svg("text", { x, y: bottom + 20, "text-anchor": "middle" });
      label.textContent = frequency === "day" ? dateLabel(point.to, { day: "numeric", month: "short" })
        : dateLabel(point.to, { month: "short", ...(points.length > 7 || frequency === "span" ? { year: "2-digit" } : {}) });
      drawing.append(label);
    }

    const barWidth = Math.min(34, step * .75 / Math.max(1, bars.length));
    for (const [pointIndex, row] of segments.entries()) {
      for (const [groupIndex, column] of row.entries()) {
        const x = xAt(pointIndex) + (groupIndex - (bars.length - 1) / 2) * barWidth - barWidth / 2;
        for (const item of column) {
          const first = yAt(item.start), last = yAt(item.end);
          drawing.append(svg("rect", {
            x: x + barWidth * .07, y: Math.min(first, last), width: barWidth * .86,
            height: Math.abs(last - first), rx: style === "bar" ? 2 : 0, fill: item.color, opacity: ".9",
          }));
        }
      }
    }
    for (const series of lines) drawing.append(drawLine(points, series, xAt, yAt, yAt(0), style === "line" && series.key === "wealth"));
    const cursor = svg("g");
    drawing.append(cursor);
    layout = { width, top, bottom, left, right, xAt, yAt, lines, cursor };
    showPoint(active);
  }

  function showPoint(index) {
    active = index;
    if (!layout || !snapshot) return;
    const { width, top, bottom, xAt, yAt, lines, cursor } = layout;
    cursor.replaceChildren();
    if (index === null || !snapshot.points[index]) {
      tooltip.hidden = true;
      return;
    }
    const point = snapshot.points[index];
    const x = xAt(index);
    cursor.append(svg("line", { x1: x, x2: x, y1: top, y2: bottom, class: "crosshair" }));
    for (const series of lines) {
      const value = valueOf(point, series);
      if (value !== null) cursor.append(svg("circle", { cx: x, cy: yAt(value), r: 4.4, fill: series.color, class: "focus-dot" }));
    }
    tooltip.replaceChildren(element("strong", "", dateRange(point.from, point.to)));
    for (const series of visible) {
      const swatch = element("span", "swatch");
      swatch.style.setProperty("--swatch", series.color);
      const item = element("div", "tooltip-row");
      item.append(swatch, element("span", "", series.title), element("span", "mono", money(exact(point, series), { fixed: true })));
      tooltip.append(item);
    }
    tooltip.hidden = false;
    const position = x + 12 + tooltip.offsetWidth < width ? x + 12 : x - tooltip.offsetWidth - 12;
    tooltip.style.left = Math.max(0, Math.min(width - tooltip.offsetWidth, position)) + "px";
  }

  chart.addEventListener("pointermove", event => {
    if (!layout) return;
    const pointer = event.clientX - chart.getBoundingClientRect().left;
    const points = snapshot.points;
    const near = points.length < 2 ? 0 : Math.round((pointer - layout.xAt(0)) / (layout.xAt(1) - layout.xAt(0)));
    const index = Math.max(0, Math.min(points.length - 1, near));
    if (index !== active) showPoint(index);
  });
  chart.addEventListener("pointerleave", () => showPoint(null));
  chart.addEventListener("keydown", event => {
    if (!snapshot) return;
    if (event.key === "Escape") showPoint(null);
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const last = snapshot.points.length - 1;
    const next = event.key === "Home" ? 0 : event.key === "End" ? last
      : Math.max(0, Math.min(last, (active ?? last) + (event.key === "ArrowLeft" ? -1 : 1)));
    showPoint(next);
  });
  chart.addEventListener("blur", () => showPoint(null));
  chartData.addEventListener("change", () => {
    if (!snapshot) return;
    hiddenSeries = new Set();
    description();
    updateLegend();
    render();
    renderTable();
  });
  chartStyles.addEventListener("click", event => {
    const control = event.target.closest("[data-style]");
    if (!control) return;
    style = control.dataset.style;
    for (const item of $$("button", chartStyles)) item.setAttribute("aria-pressed", item === control);
    render();
  });
  tableToggle.addEventListener("click", () => {
    table.hidden = !table.hidden;
    tableToggle.setAttribute("aria-expanded", String(!table.hidden));
    renderTable();
  });
  new ResizeObserver(render).observe(chart);

  return {
    update(data) {
      snapshot = data;
      active = null;
      const noHistory = !data.accounts.length && data.shared_balance === null;
      $("#trend-empty").hidden = !noHistory;
      $("#trend-content").hidden = noHistory;
      for (const control of [chartData, ...$$("button", chartStyles)]) control.disabled = noHistory;
      description();
      updateLegend();
      render();
      renderTable();
    },
  };
}
