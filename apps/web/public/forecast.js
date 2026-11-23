import { api, apiMessage, button, categoryPath, dateLabel, element, money, options, svg } from "./ui.js";
import { mayOpenProfile } from "./workspaces.js";

const base = "/api/forecast/";
const liquid = new Set(["cash", "checking", "savings"]);
const modes = {
  runway: "Cash runway",
  simulator: "What-if simulator",
  actual: "Plan vs. actual",
};
const types = [
  ["income", "Income"], ["expense", "Expense"],
  ["investment", "Investment contribution"], ["transfer", "Account transfer"],
];
const cadences = [
  ["once", "One time"], ["weekly", "Weekly"], ["monthly", "Monthly"], ["yearly", "Yearly"],
];
const purposes = [
  ["payday", "Payday"], ["bill", "Bill"], ["subscription", "Subscription"], ["commitment", "Other commitment"],
];
const returnPurpose = "return";
const monthOptions = { month: "short", year: "numeric" };
const plotColors = ["#5647df", "#148275", "#b14b70"];

function iso(day) {
  return day.toISOString().slice(0, 10);
}

function offsetMonth(month, delta) {
  const day = new Date(month + "-01T00:00:00Z");
  day.setUTCMonth(day.getUTCMonth() + delta);
  return iso(day).slice(0, 7);
}

function monthText(month) {
  return dateLabel(month + "-01", monthOptions);
}

function field(form, name, caption, attrs = {}) {
  const { pairs, ...properties } = attrs;
  const input = element(pairs ? "select" : "input");
  input.name = name;
  Object.assign(input, properties);
  if (pairs) options(input, pairs, attrs.value);
  const label = element("label", "field", element("span", "", caption));
  label.append(input);
  form.append(label);
  return input;
}

function notice(error) {
  const node = element("p", "notice", typeof error === "string" ? error : apiMessage(error));
  node.setAttribute("role", "alert");
  return node;
}

function loadInto(target, task, render) {
  target.replaceChildren(element("p", "muted", "Loading…"));
  return task.then(data => {
    if (target.isConnected) render(data);
  }).catch(error => { if (target.isConnected) target.replaceChildren(notice(error)); });
}

function panel(title) {
  return element("section", "panel forecast-panel", element("h2", "", title));
}

function submit(form, label) {
  const control = button(label, "primary");
  control.type = "submit";
  form.append(element("div", "form-actions", control));
  return control;
}

function table(headers, rows) {
  const grid = element("table");
  const head = element("tr");
  head.append(...headers.map(header => {
    const cell = element("th", "", header);
    cell.scope = "col";
    return cell;
  }));
  const body = element("tbody");
  body.append(...rows.map(values => {
    const row = element("tr");
    row.append(...values.map(value => element("td", "", value)));
    return row;
  }));
  grid.append(element("thead", "", head), body);
  return element("div", "table-scroll", grid);
}

function plot(container, points, keys, reserve = null, breaches = []) {
  if (container.forecastObserver) container.forecastObserver.disconnect();
  container.replaceChildren();
  if (points.length === 0) return;
  const plotArea = element("div", "forecast-plot");
  const chart = svg("svg", { role: "img", "aria-label": keys.map(key => key.label).join(", ") });
  const legend = element("div", "forecast-legend");
  keys.forEach(({ label, dashed }, index) => {
    const swatch = element("span", dashed ? "forecast-swatch dashed" : "forecast-swatch");
    swatch.style.color = plotColors[index];
    const item = element("span", "", swatch);
    item.append(label);
    legend.append(item);
  });
  if (reserve !== null) {
    const item = element("span", "", element("span", "forecast-swatch reserve"));
    item.append("Cash reserve " + money(reserve));
    legend.append(item);
  }
  plotArea.append(chart);
  container.append(plotArea, legend);
  const amounts = points.flatMap(point => keys.map(key => point[key.value]))
    .filter(value => value !== null && value !== undefined).map(Number);
  if (reserve !== null) amounts.push(Number(reserve));
  if (!amounts.length) return;
  const lowest = Math.min(...amounts);
  const highest = Math.max(...amounts);
  const padding = Math.max(100, (highest - lowest) * .11);
  const minimum = lowest - padding;
  const maximum = highest + padding;
  const closeFormat = new Intl.NumberFormat(undefined, { style: "currency", currency: "EUR",
    notation: "compact", maximumFractionDigits: 1 });
  const dates = points.map(point => Date.parse(point.day + "T00:00:00Z"));
  function paint() {
    if (!container.isConnected) {
      container.forecastObserver?.disconnect();
      return;
    }
    const width = plotArea.clientWidth;
    if (!width) return;
    const height = plotArea.clientHeight;
    const left = width < 360 ? 53 : 68, right = width - 15;
    const top = 10, bottom = height - 31;
    const dateX = date => right > left && dates.at(-1) > dates[0]
      ? left + (date - dates[0]) / (dates.at(-1) - dates[0]) * (right - left)
      : (left + right) / 2;
    const x = index => dateX(dates[index]);
    const y = amount => bottom - (Number(amount) - minimum) / (maximum - minimum) * (bottom - top);
    chart.setAttribute("viewBox", "0 0 " + width + " " + height);
    const marks = [];
    const line = (x1, y1, x2, y2, attrs) => svg("line", { x1, y1, x2, y2, ...attrs });
    function text(label, x, y, anchor) {
      const node = svg("text", { x, y, "text-anchor": anchor, class: "forecast-tick" });
      node.textContent = label;
      return node;
    }
    for (let i = 0; i <= 4; i++) {
      const value = minimum + (maximum - minimum) * i / 4;
      marks.push(line(left, y(value), right, y(value), { class: "forecast-gridline" }),
        text(closeFormat.format(value / 100), left - 7, y(value) + 4, "end"));
    }
    const ticks = Math.min(points.length, width < 420 ? 3 : 6);
    for (let tick = 0; tick < ticks; tick++) {
      const index = ticks === 1 ? 0 : Math.round(tick * (points.length - 1) / (ticks - 1));
      marks.push(text(points[index].opening ? "Today" : dateLabel(points[index].day, { month: "short", day: width > 540 ? "numeric" : undefined }),
        x(index), bottom + 20, index === 0 ? "start" : index === points.length - 1 ? "end" : "middle"));
    }
    if (reserve !== null) marks.push(line(left, y(reserve), right, y(reserve), { class: "forecast-reserve-line" }));
    for (const [index, series] of keys.entries()) {
      let path = "";
      let connecting = false;
      for (const [pointIndex, point] of points.entries()) {
        const value = point[series.value];
        if (value === null || value === undefined) { connecting = false; continue; }
        path += (connecting ? " L " : " M ") + x(pointIndex) + " " + y(value);
        connecting = true;
        if (points.length <= 25 && !point.opening) {
          const dot = svg("circle", { cx: x(pointIndex), cy: y(value), r: 3.4, fill: plotColors[index] });
          const label = svg("title");
          label.textContent = dateLabel(point.day) + " · " + series.label + " " + money(value);
          dot.append(label);
          marks.push(dot);
        }
      }
      marks.push(svg("path", { d: path, fill: "none", stroke: plotColors[index], "stroke-width": 2.5,
        "stroke-dasharray": series.dashed ? "7 5" : "none", "stroke-linejoin": "round", "pointer-events": "none" }));
    }
    if (reserve !== null) breaches.forEach((day, index) => {
      if (!day) return;
      const position = dateX(Date.parse(day + "T00:00:00Z"));
      const marker = svg("circle", { cx: position, cy: y(reserve), r: 5 + index * 2, fill: "none",
        stroke: plotColors[index], "stroke-width": 2.3 });
      const label = svg("title");
      label.textContent = keys[index].label + " first below reserve on " + dateLabel(day);
      marker.append(label);
      marks.push(line(position, top, position, bottom, {
        stroke: plotColors[index], "stroke-dasharray": "4 4", "stroke-opacity": ".4",
      }), marker);
    });
    chart.replaceChildren(...marks);
  }
  container.forecastObserver = new ResizeObserver(paint);
  container.forecastObserver.observe(plotArea);
  paint();
}

export function setupForecast(root, { onChange = () => {}, notify = () => {}, checkFunding = () => {} } = {}) {
  const tabs = element("div", "segmented forecast-modes");
  tabs.setAttribute("aria-label", "Forecast tool");
  tabs.setAttribute("role", "group");
  const view = element("div", "forecast-view");
  let currentMode = "runway", setup, viewedMonth, selectedDay, updateGeneration = 0, actualPlan;
  const controls = new Map();
  for (const [mode, label] of Object.entries(modes)) {
    const tab = button(label, "");
    tab.addEventListener("click", () => open(mode));
    tabs.append(tab);
    controls.set(mode, tab);
  }
  const header = element("header", "page-heading");
  const heading = element("div", "", element("h1", "", "Forecast"));
  heading.append(element("p", "page-description", "Plan near-term commitments and review saved decisions against your ledger."));
  const editReturns = element("a", "secondary", "Monthly returns");
  editReturns.href = "#returns";
  header.append(heading, editReturns);
  root.replaceChildren(header, tabs, view);

  async function open(mode = currentMode, target) {
    currentMode = Object.hasOwn(modes, mode) ? mode : currentMode;
    root.dataset.helpContext = currentMode;
    actualPlan = currentMode === "actual" ? target : undefined;
    if (target && currentMode === "runway") {
      selectedDay = target;
      viewedMonth = target.slice(0, 7);
    }
    for (const [name, tab] of controls) tab.setAttribute("aria-pressed", String(name === currentMode));
    const generation = ++updateGeneration;
    view.replaceChildren(element("p", "muted", "Loading forecast…"));
    try {
      const data = await api(base + "setup");
      if (generation !== updateGeneration) return;
      setup = data;
      view.replaceChildren();
      if (currentMode === "runway") openRunway();
      else if (currentMode === "simulator") openSimulator();
      else openActual();
    } catch (error) {
      if (generation === updateGeneration) view.replaceChildren(notice(error));
    }
  }

  function openRunway() {
    viewedMonth ??= setup.today.slice(0, 7);
    const calendarPanel = panel("Daily cash runway");
    const month = element("input");
    month.type = "month";
    month.value = viewedMonth;
    month.min = "1900-01";
    month.max = "9998-12";
    month.setAttribute("aria-label", "Calendar month");
    const previous = button("Previous month");
    const next = button("Next month");
    const navigation = element("div", "forecast-actions");
    const funding = button("Check funding");
    funding.addEventListener("click", () => checkFunding({ amount: "0", purchase_on: selectedDay, label: "Scheduled payments" }));
    navigation.append(previous, month, next, funding);
    calendarPanel.append(navigation);
    calendarPanel.append(element("p", "muted", "Daily closing cash includes checking and savings. Pending commitments dated before today appear on the calendar; post or match them to update cash."));
    const body = element("div", "forecast-calendar-body"), rulesPanel = panel("Commitments");
    calendarPanel.append(body);
    view.append(calendarPanel, rulesPanel);
    buildCommitmentForm(rulesPanel);
    let request = 0;
    const selectMonth = value => {
      if (!/^\d{4}-\d{2}$/.test(value) || value < "1900-01" || value > "9998-12") return;
      viewedMonth = value;
      month.value = value;
      previous.disabled = value === "1900-01";
      next.disabled = value === "9998-12";
      const generation = ++request;
      loadInto(body, api(base + "runway?month=" + encodeURIComponent(value)), data => {
        if (generation === request) drawCalendar(body, data, () => selectMonth(viewedMonth));
      });
    };
    previous.addEventListener("click", () => selectMonth(offsetMonth(viewedMonth, -1)));
    next.addEventListener("click", () => selectMonth(offsetMonth(viewedMonth, 1)));
    month.addEventListener("change", () => selectMonth(month.value));
    selectMonth(viewedMonth);
  }

  function drawCalendar(body, data, reload) {
    const grid = element("div", "forecast-calendar");
    const inspect = element("div", "forecast-day-detail");
    const chart = element("div", "forecast-graph");
    const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    grid.append(...weekdays.map(day => element("span", "forecast-weekday muted", day)));
    const first = new Date(data.month + "-01T00:00:00Z");
    const offset = (first.getUTCDay() + 6) % 7;
    for (let i = 0; i < offset; i++) grid.append(element("span"));
    if (!selectedDay || !selectedDay.startsWith(data.month)) {
      selectedDay = data.today.startsWith(data.month) ? data.today : data.days[0].date;
    }
    const buttons = new Map();
    const inspectDate = day => {
      selectedDay = day.date;
      for (const [date, node] of buttons) node.setAttribute("aria-pressed", String(date === day.date));
      const balance = [day.actual !== null ? "Actual " + money(day.actual) : "",
        day.projected !== null ? "Projected " + money(day.projected) : ""].filter(Boolean).join(" · ");
      const dayHeading = element("h3", "", dateLabel(day.date));
      inspect.replaceChildren(dayHeading, element("p", "muted", balance || "No cash accounts in this period."));
      for (const item of day.items) inspect.append(occurrenceRow(item, day, data.today, reload));
      for (const item of day.shared) {
        const entry = element("div", "forecast-occurrence");
        const detail = element("div", "", element("strong", "", item.name));
        const movement = BigInt(item.amount);
        const amounts = item.kind === "settlement"
          ? ["Scheduled " + (movement > 0n ? "cash received " : "cash repaid ") + money(movement > 0n ? movement : -movement)]
          : ["Scheduled", "Cash paid " + money(-movement), "Your share " + money(item.personal_amount),
              "Owed back " + money(item.owed_back)];
        detail.append(element("p", "muted", amounts.join(" · ")));
        entry.append(detail);
        if (movement < 0n) entry.append(fundingAction(item, day));
        inspect.append(entry);
      }
      if (!(day.items.length || day.shared.length)) inspect.append(element("p", "muted", "No scheduled activity."));
    };
    for (const day of data.days) {
      const value = day.projected ?? day.actual;
      const count = day.items.length + day.shared.length;
      const returnCount = day.items.filter(item => item.purpose === returnPurpose && item.status !== "skipped").length;
      const control = button(String(Number(day.date.slice(-2))), "forecast-day");
      control.dataset.date = day.date;
      control.setAttribute("aria-label", dateLabel(day.date) + (value === null ? "" : ", " + money(value))
        + (count ? ", " + count + " commitments" : "") + (returnCount ? ", " + returnCount + " investment returns" : ""));
      if (day.date === data.today) control.setAttribute("aria-current", "date");
      if (value !== null) {
        const label = element("small", BigInt(value) < 0n ? "forecast-outflow" : "", money(value));
        control.append(label);
      }
      if (count) {
        const markers = element("span", "forecast-markers");
        if (returnCount) {
          const marker = element("span", "forecast-return-dot");
          marker.title = "Monthly return";
          markers.append(marker);
        }
        if (count > returnCount) markers.append(element("span", "forecast-commitment-dot"));
        control.append(markers);
      }
      control.addEventListener("click", () => inspectDate(day));
      buttons.set(day.date, control);
      grid.append(control);
    }
    body.replaceChildren(grid, inspect, chart);
    inspectDate(data.days.find(day => day.date === selectedDay) ?? data.days[0]);
    const points = data.days.map(day => ({ day: day.date, actual: day.actual, projected: day.projected }));
    if (points.some(point => point.actual !== null || point.projected !== null)) {
      plot(chart, points, [{ value: "actual", label: "Actual cash" }, { value: "projected", label: "Projected cash", dashed: true }]);
    }
  }

  function fundingAction(item, day) {
    const check = button("Check funding", "text-button");
    check.addEventListener("click", () => checkFunding({
      account_id: item.account_id, amount: "0", purchase_on: day.date, label: item.name,
    }));
    return check;
  }

  function occurrenceRow(item, day, today, reload) {
    const row = element("div", "forecast-occurrence");
    const detail = element("div", "", element("strong", "", item.name));
    const isReturn = item.purpose === returnPurpose;
    const purpose = isReturn ? "Monthly return" : purposes.find(([key]) => key === item.purpose)?.[1];
    detail.append(element("p", "muted", [purpose, money(item.amount), item.status === "posted"
      ? "In ledger" : item.status === "skipped" ? "Skipped" : "Scheduled"].filter(Boolean).join(" · ")));
    if (isReturn) {
      row.classList.add("forecast-return-occurrence");
      const payout = setup.accounts.find(account => account.id === item.account_id);
      if (payout) detail.append(element("p", "muted", "Credits " + payout.name +
        (liquid.has(payout.kind) ? " · cash account" : " · investment balance")));
    }
    row.append(detail);
    if (item.kind === "expense" && item.status === "pending" && day.date >= today) {
      row.append(fundingAction(item, day));
    }
    if (item.profile_id) {
      const canOpen = mayOpenProfile(item.profile_id);
      const link = element(canOpen ? "a" : "span", canOpen ? "text-link" : "muted", item.profile_name + " schedule");
      if (canOpen) link.href = "/?workspace=" + item.profile_id + "#forecast/runway/" + day.date;
      row.append(link);
      return row;
    }
    if (item.status === "posted") return row;
    const actions = element("div", "forecast-actions");
    row.append(actions);
    const send = async data => {
      for (const child of actions.querySelectorAll("button")) child.disabled = true;
      try {
        await api(base + "occurrences", { method: "POST", data: { rule_id: item.id, date: day.date, ...data } });
        onChange();
        reload();
      } catch (error) {
        row.append(notice(error));
        for (const child of actions.querySelectorAll("button")) child.disabled = false;
      }
    };
    if (item.status === "skipped") {
      const restore = button("Restore");
      restore.addEventListener("click", () => send({ restore: true }));
      actions.append(restore);
      return row;
    }
    if (day.date <= today) {
      const post = button(isReturn ? "Record return" : "Post", "primary");
      const link = button("Match");
      post.addEventListener("click", () => send({}));
      link.addEventListener("click", () => {
        if (row.querySelector(".forecast-match")) return;
        const matcher = element("div", "forecast-match");
        row.append(matcher);
        const query = new URLSearchParams({ rule_id: item.id, date: day.date });
        loadInto(matcher, api(base + "matches?" + query), entries => {
          if (!entries.length) {
            matcher.replaceChildren(element("p", "muted", "No unmatched transactions with these accounts and type within 14 days."));
            return;
          }
          const form = element("form", "forecast-actions");
          const id = field(form, "entry", "Existing transaction", {
            pairs: entries.map(entry => [entry.id, entry.name + " · " + dateLabel(entry.posted_on) + " · " + money(entry.amount)]),
          });
          submit(form, "Link transaction");
          form.addEventListener("submit", event => { event.preventDefault(); send({ entry_id: id.value }); });
          matcher.replaceChildren(form);
        });
      });
      actions.append(post, link);
    }
    const skip = button("Skip occurrence");
    skip.addEventListener("click", () => send({ skip: true }));
    actions.append(skip);
    return row;
  }

  function buildCommitmentForm(container) {
    const accounts = setup.accounts.filter(account => liquid.has(account.kind) && !account.profile_id);
    const rules = setup.rules.filter(rule => !rule.archived_on);
    if (!accounts.length) {
      container.append(element("p", "muted", rules.some(rule => rule.profile_id)
        ? "Open a member’s schedule to manage their commitments. Add a joint cash account to create group commitments."
        : "Add a cash, checking, or savings account before scheduling commitments."));
      appendRules(container, rules);
      return;
    }
    const disclosure = element("details", "forecast-commitment-form", element("summary", "", "Add a commitment"));
    const form = element("form", "field-grid");
    const issue = element("div", "forecast-span");
    const categoryMap = new Map(setup.categories.map(item => [item.id, item]));
    field(form, "name", "Name", { required: true, maxLength: 100, placeholder: "e.g. Rent" });
    const purpose = field(form, "purpose", "What is this for?", { pairs: purposes, value: "bill" });
    const kind = field(form, "kind", "Transaction type", { pairs: types, value: "expense" });
    field(form, "cadence", "Repeats", { pairs: cadences, value: "monthly" });
    field(form, "amount", "Amount in euros", { required: true, inputMode: "decimal", placeholder: "0.00" });
    const source = field(form, "account_id", "Cash or savings account", { pairs: accounts.map(account => [account.id, account.name]) });
    const destination = field(form, "destination_id", "Destination account", { pairs: [] });
    const category = field(form, "category_id", "Category", { pairs: [] });
    field(form, "start_on", "First date", { type: "date", required: true, value: setup.today });
    field(form, "end_on", "Last date (optional)", { type: "date" });
    function updateChoices() {
      const moves = ["transfer", "investment"].includes(kind.value);
      destination.parentElement.hidden = !moves;
      destination.disabled = !moves;
      options(destination, setup.accounts.filter(account => !account.profile_id && String(account.id) !== source.value
        && (kind.value === "investment" ? account.kind === "investment" : liquid.has(account.kind)))
        .map(account => [account.id, account.name]), undefined, "Choose destination");
      options(category, setup.categories.filter(item => !item.profile_id && item.kind === kind.value)
        .map(item => [item.id, categoryPath(item, categoryMap)]), undefined, "Uncategorized");
    }
    kind.addEventListener("change", updateChoices);
    source.addEventListener("change", updateChoices);
    purpose.addEventListener("change", () => {
      if (purpose.value !== "commitment") kind.value = purpose.value === "payday" ? "income" : "expense";
      updateChoices();
    });
    updateChoices();
    form.append(issue);
    const save = submit(form, "Save commitment");
    form.addEventListener("submit", async event => {
      event.preventDefault();
      save.disabled = true;
      issue.replaceChildren();
      try {
        await api(base + "commitments", { method: "POST", data: Object.fromEntries(new FormData(form)) });
        notify("Commitment saved.");
        await open("runway");
      } catch (error) {
        issue.replaceChildren(notice(error));
      } finally { save.disabled = false; }
    });
    disclosure.append(form);
    container.append(disclosure);
    if (!rules.length) container.append(element("p", "muted", "Add payday, bills, subscriptions, or a planned investment contribution to start projecting cash."));
    appendRules(container, rules);
  }

  function appendRules(container, rules) {
    for (const rule of rules) {
      const row = element("div", "forecast-occurrence");
      const title = element("div", "", element("strong", "", rule.name));
      title.append(element("p", "muted", money(rule.amount) + " · "
        + cadences.find(([key]) => key === rule.cadence)?.[1] + " · from " + dateLabel(rule.start_on)));
      if (rule.profile_id) {
        const canOpen = mayOpenProfile(rule.profile_id);
        const link = element(canOpen ? "a" : "span", canOpen ? "text-link" : "muted", rule.profile_name + " schedule");
        if (canOpen) link.href = "/?workspace=" + rule.profile_id + "#forecast";
        row.append(title, link);
        container.append(row);
        continue;
      }
      if (rule.purpose === returnPurpose) {
        const terms = element("a", "text-link", "Manage return");
        terms.href = "#returns";
        row.append(title, terms);
        container.append(row);
        continue;
      }
      const archive = button("End schedule");
      archive.addEventListener("click", async () => {
        archive.disabled = true;
        try {
          await api(base + "commitments/" + rule.id, { method: "DELETE" });
          notify("Schedule ended.");
          await open("runway");
        } catch (error) { row.append(notice(error)); archive.disabled = false; }
      });
      row.append(title, archive);
      container.append(row);
    }
  }

  function openSimulator() {
    const assumptionPanel = panel("Compare two cash forecasts");
    const report = panel("Forecast comparison");
    const result = element("div", "forecast-result");
    report.append(result);
    const saved = panel("Saved what-if comparisons");
    const savedList = element("div");
    saved.append(savedList);
    view.append(assumptionPanel, report, saved);
    if (!setup.accounts.some(account => liquid.has(account.kind))) {
      assumptionPanel.append(element("p", "muted", "Add a cash, checking, or savings account to simulate changes against your ledger."));
      report.hidden = true;
      return;
    }
    const form = element("form", "field-grid");
    const issue = element("div", "forecast-span");
    assumptionPanel.append(element("p", "muted", "Uses cash and savings, upcoming commitments, and scheduled shared cash payments. Both paths include the modeled purchase; only the alternative uses the sliders."), form);
    const horizon = field(form, "months", "Horizon", { pairs: [[3, "3 months"], [6, "6 months"], [12, "12 months"], [24, "24 months"]], value: "12" });
    field(form, "reserve", "Cash reserve in euros", { value: "0", inputMode: "decimal", required: true });
    field(form, "purchase_amount", "Purchase amount in euros", { value: "0", inputMode: "decimal", required: true });
    const purchaseDate = field(form, "purchase_on", "Baseline purchase date", {
      type: "date", value: offsetMonth(setup.today.slice(0, 7), 1) + "-01", min: setup.today, required: true,
    });
    const shift = field(form, "purchase_shift", "Alternative purchase timing", { type: "range", min: -1, max: 10, step: 1, value: 0 });
    const rentRule = field(form, "rent_rule", "Recurring expense to increase", {
      pairs: [["", "Choose rent or another expense"], ...setup.rules.filter(rule => !rule.archived_on
        && rule.kind === "expense" && rule.cadence !== "once").map(rule => [rule.id, rule.name]),
      ...setup.shared_rules.map(rule => ["shared:" + rule.id, rule.name + " · shared payment"])],
    });
    const rent = field(form, "rent_percent", "Alternative recurring expense increase", { type: "range", min: 0, max: 100, value: 0 });
    const investment = field(form, "investment_delta", "Alternative monthly investment change", {
      type: "range", min: -2500, max: 2500, step: 25, value: 0,
    });
    const name = field(form, "name", "Comparison name", { maxLength: 80, placeholder: "e.g. Wait three months" });
    const forecast = button("Update comparison", "secondary");
    const save = button("Save comparison", "primary");
    const actions = element("div", "forecast-actions");
    actions.append(forecast, save);
    form.append(issue, actions);
    form.addEventListener("submit", event => { event.preventDefault(); simulate(); });

    function describeRange(input, description) {
      const value = element("output", "muted", description);
      input.parentElement.append(value);
      return value;
    }
    const shiftLabel = describeRange(shift, "Same date");
    const rentLabel = describeRange(rent, "0% increase");
    const investmentLabel = describeRange(investment, "€0 per month");
    investment.parentElement.append(element("small", "", "Increases apply at month-end. Reductions lower scheduled contributions when due."));
    function syncSliders() {
      const thisMonth = setup.today.slice(0, 7);
      const horizonEnd = offsetMonth(thisMonth, Number(horizon.value));
      const last = new Date(horizonEnd + "-01T00:00:00Z");
      last.setUTCDate(0);
      purchaseDate.max = iso(last);
      if (purchaseDate.value && purchaseDate.value > purchaseDate.max) purchaseDate.value = purchaseDate.max;
      if (purchaseDate.value) {
        const [thisYear, thisMonthNumber] = thisMonth.split("-").map(Number);
        const [year, month, day] = purchaseDate.value.split("-").map(Number);
        const distance = (year - thisYear) * 12 + month - thisMonthNumber;
        const earliest = -distance + (day < Number(setup.today.slice(-2)) ? 1 : 0);
        shift.min = String(Math.min(0, earliest));
        shift.max = String(Math.max(0, Number(horizon.value) - distance - 1));
      }
      rent.disabled = !rentRule.value;
      if (!rentRule.value) rent.value = "0";
      const months = Math.abs(Number(shift.value));
      shiftLabel.textContent = months ? months + (months === 1 ? " month " : " months ") + (Number(shift.value) < 0 ? "earlier" : "later") : "Same date";
      rentLabel.textContent = rent.value + "% increase";
      investmentLabel.textContent = money(BigInt(investment.value) * 100n) + " per month";
    }

    function readInputs() {
      return {
        months: Number(horizon.value),
        reserve: form.elements.reserve.value,
        purchase_amount: form.elements.purchase_amount.value,
        purchase_on: purchaseDate.value,
        purchase_shift: Number(shift.value),
        rent_rule: rentRule.value,
        rent_percent: Number(rent.value),
        investment_delta: investment.value,
      };
    }

    let ticket = 0, timer;
    let saving = false;
    let validComparison = false;
    async function simulate() {
      clearTimeout(timer);
      if (!result.isConnected) return;
      const sequence = ++ticket;
      issue.replaceChildren();
      save.disabled = true;
      validComparison = false;
      try {
        const data = await api(base + "simulate", { method: "POST", data: readInputs() });
        if (sequence !== ticket || !result.isConnected) return;
        drawSimulation(result, data);
        validComparison = true;
      } catch (error) {
        if (sequence === ticket) issue.replaceChildren(notice(error));
      } finally {
        if (sequence === ticket) save.disabled = saving || !validComparison;
      }
    }
    form.addEventListener("input", ({ target }) => {
      if (target === name) return;
      syncSliders();
      clearTimeout(timer);
      validComparison = false;
      save.disabled = true;
      ++ticket;
      timer = setTimeout(simulate, 135);
    });
    forecast.addEventListener("click", simulate);
    save.addEventListener("click", async () => {
      if (!name.value.trim()) { name.setCustomValidity("Name the comparison before saving."); name.reportValidity(); return; }
      saving = save.disabled = true;
      issue.replaceChildren();
      try {
        await api(base + "comparisons", { method: "POST", data: { name: name.value, inputs: readInputs() } });
        notify("Comparison saved.");
        name.value = "";
        refreshSaved();
      } catch (error) { issue.replaceChildren(notice(error)); }
      finally { saving = false; save.disabled = !validComparison; }
    });
    name.addEventListener("input", () => name.setCustomValidity(""));
    function refreshSaved() {
      loadInto(savedList, api(base + "comparisons"), items => {
        if (!items.length) { savedList.replaceChildren(element("p", "muted", "Saved comparisons appear here.")); return; }
        savedList.replaceChildren(...items.map(item => {
          const row = element("div", "forecast-occurrence");
          const title = element("div", "", element("strong", "", item.name));
          title.append(element("p", "muted", dateLabel(item.saved_on)));
          const openButton = button("View saved");
          openButton.addEventListener("click", () => {
            ++ticket;
            validComparison = false;
            save.disabled = true;
            clearTimeout(timer);
            loadInto(result, api(base + "comparisons/" + item.id), record => drawSimulation(result, record.result, record.name));
          });
          const remove = button("Delete");
          remove.setAttribute("aria-label", "Delete " + item.name);
          remove.addEventListener("click", async () => {
            if (!window.confirm("Delete the saved " + item.name + " comparison?")) return;
            try {
              await api(base + "comparisons/" + item.id, { method: "DELETE" });
              refreshSaved();
            } catch (error) { row.append(notice(error)); }
          });
          const controls = element("div", "forecast-actions");
          controls.append(openButton, remove);
          row.append(title, controls);
          return row;
        }));
      });
    }
    syncSliders();
    simulate();
    refreshSaved();
  }

  function drawSimulation(root, data, savedName) {
    const chart = element("div", "forecast-graph");
    const summary = element("div", "forecast-breach");
    if (savedName) root.replaceChildren(element("h3", "", savedName), chart, summary);
    else root.replaceChildren(chart, summary);
    const reserveLabel = date => date ? "First below reserve on " + dateLabel(date) : "Stays at or above reserve";
    for (const [label, date, purchase] of [
      ["Baseline", data.baseline_breach, data.purchase_on],
      ["Alternative", data.alternative_breach, data.alternative_purchase_on],
    ]) {
      const card = element("div", "", element("strong", "", label));
      card.append(element("p", date ? "forecast-outflow" : "muted", reserveLabel(date)));
      if (purchase) card.append(element("p", "muted", "Purchase " + dateLabel(purchase)));
      summary.append(card);
    }
    plot(chart, data.points, [{ value: "baseline", label: "Baseline" }, { value: "alternative", label: "Alternative", dashed: true }],
      data.reserve, [data.baseline_breach, data.alternative_breach]);
    const details = element("details", "", element("summary", "", "Monthly balances"));
    details.append(table(["Date", "Baseline", "Alternative"], data.points.map(point =>
      [dateLabel(point.day) + (point.opening ? " opening" : ""), money(point.baseline), money(point.alternative)])));
    root.append(details);
  }

  function openActual() {
    const inspector = panel("Reopen a saved forecast");
    const content = element("div");
    view.append(inspector, content);
    loadInto(inspector, Promise.all([api("/api/scenarios"), api(base + "comparisons")]), ([plans, simulations]) => {
      const list = [...plans.map(plan => ["plan:" + plan.id, plan.name + " · purchase plan"]),
        ...simulations.map(plan => ["simulation:" + plan.id, plan.name + " · what-if"])];
      if (!list.length) {
        inspector.replaceChildren(element("h2", "", "Reopen a saved forecast"),
          element("p", "muted", "Save a purchase plan or what-if comparison to track it against your actual cash balances."));
        return;
      }
      const controls = element("div", "forecast-actions");
      const selected = field(controls, "comparison", "Saved forecast", { pairs: list });
      if (actualPlan && list.some(([value]) => value === "plan:" + actualPlan)) selected.value = "plan:" + actualPlan;
      inspector.replaceChildren(element("h2", "", "Reopen a saved forecast"), controls);
      let request = 0;
      function loadPlan() {
        const [kind, id] = selected.value.split(":");
        const path = kind === "plan" ? "actual/" : "actual-simulation/";
        const generation = ++request;
        loadInto(content, api(base + path + id), data => {
          if (generation !== request) return;
          if (kind === "plan") showActualPlan(content, data);
          else showActualSimulation(content, data);
        });
      }
      selected.addEventListener("change", loadPlan);
      loadPlan();
    });
  }

  function showActualPlan(root, data) {
    const chartPanel = panel(data.name);
    root.replaceChildren(chartPanel);
    if (!data.points.length) {
      chartPanel.append(element("p", "muted", "No feasible funding schedule was saved for this plan."));
      return;
    }
    chartPanel.append(element("p", "muted", "Saved and recorded month-end cash and savings. Actuals appear for completed months with ledger history."));
    const chart = element("div", "forecast-graph");
    chartPanel.append(chart);
    plot(chart, data.points, [{ value: "plan", label: "Saved plan" }, { value: "actual", label: "Ledger cash", dashed: true }]);
    const observed = data.points.filter(point => point.actual !== null);
    if (!observed.length) {
      chartPanel.append(element("p", "muted", "There are no completed plan months with recorded balances yet."));
      return;
    }
    const detail = panel("Monthly comparison");
    root.append(detail);
    const selectors = element("div", "forecast-actions");
    const month = field(selectors, "month", "Month to review", {
      pairs: observed.map(point => [point.month, monthText(point.month)]), value: observed.at(-1).month,
    });
    const range = field(selectors, "range", "Category activity", {
      pairs: [["cumulative", "Through this month"], ["month", "This month only"]],
    });
    const amounts = element("div", "forecast-result");
    detail.append(selectors, amounts);
    function showMonth() {
      const point = observed.find(item => item.month === month.value);
      const variance = BigInt(point.actual) - BigInt(point.plan);
      amounts.replaceChildren(table(["Saved balance", "Actual balance", "Cash difference"],
        [[money(point.plan), money(point.actual), money(variance)]]));
      const lines = (range.value === "cumulative" ? data.categories_cumulative : data.categories)[month.value];
      if (!lines) {
        amounts.append(element("p", "muted", "The starting month contains the plan’s opening assumptions."));
        return;
      }
      const caption = data.has_targets
        ? "Category differences compare the estimates saved with the plan to direct category activity. Timing, settlements and other cash movements can also affect the balance gap."
        : "This plan has no saved category estimates. Recorded activity is available for review.";
      amounts.append(element("p", "muted", caption));
      if (!lines.length) { amounts.append(element("p", "muted", "No direct category activity in this month.")); return; }
      amounts.append(table(["Category", "Activity", "Saved estimates", "Actual", "Estimated cash impact"],
        lines.map(line => [line.name, types.find(([kind]) => line.kind === kind)?.[1] ?? line.kind,
          money(line.planned), money(line.actual), money(line.variance)])));
    }
    month.addEventListener("change", showMonth);
    range.addEventListener("change", showMonth);
    showMonth();
  }

  function showActualSimulation(root, record) {
    const card = panel(record.name);
    const chart = element("div", "forecast-graph");
    root.replaceChildren(card);
    const points = record.result.points;
    card.append(element("p", "muted", "Saved forecasts and actual month-end cash on the original account scope."), chart);
    plot(chart, points, [{ value: "baseline", label: "Saved baseline" }, { value: "alternative", label: "Saved alternative", dashed: true },
      { value: "actual", label: "Ledger cash" }]);
    const observed = points.filter(point => point.actual !== null);
    if (!observed.length) card.append(element("p", "muted", "There are no completed comparison months with recorded balances yet."));
    else card.append(table(["Month", "Saved baseline", "Saved alternative", "Actual"], observed.map(point =>
      [monthText(point.month), money(point.baseline), money(point.alternative), money(point.actual)])));
  }

  return { open };
}
