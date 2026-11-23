import { $, api, apiMessage, button, dateLabel, element, money, options, svg } from "./ui.js";

const base = "/api/decision/";
const modes = { risk: "Risk simulator", optimize: "Decision optimizer", tradeoff: "Goal trade-off", score: "Forecast scorecard" };
const percent = value => value == null ? "—" : Number(value).toLocaleString(undefined, { maximumFractionDigits: 1 }) + "%";
const signedMoney = value => value != null && BigInt(value) > 0n ? "+" + money(value) : money(value);
const safeDate = value => value ? dateLabel(value) : "—";
const compactMoney = new Intl.NumberFormat("en", { style: "currency", currency: "EUR", notation: "compact", maximumFractionDigits: 1 });

function panel(title, text) {
  const node = element("section", "panel decision-panel");
  node.append(element("h2", "", title));
  if (text) node.append(element("p", "muted", text));
  return node;
}

function field(wrapper, name, label, value, config = {}) {
  const entry = element("label", "field");
  const input = element(config.items ? "select" : "input");
  input.name = name;
  if (config.items) options(input, config.items, value);
  else {
    input.type = config.type ?? "text";
    input.value = config.type === "checkbox" ? "yes" : value ?? "";
    if (config.type === "checkbox") input.checked = Boolean(value);
    for (const key of ["min", "max", "step", "placeholder", "required", "inputMode"]) {
      if (key in config) input[key] = config[key];
    }
  }
  entry.append(element("span", "", label), input);
  wrapper.append(entry);
  return input;
}

function table(columns, rows) {
  const node = element("table");
  const header = element("tr");
  for (const caption of columns) {
    const cell = element("th", "", caption);
    cell.scope = "col";
    header.append(cell);
  }
  const body = element("tbody");
  for (const row of rows) {
    const line = element("tr");
    line.append(...row.map(value => element("td", "", String(value ?? "—"))));
    body.append(line);
  }
  node.append(element("thead", "", header), body);
  return element("div", "table-scroll", node);
}

function message(node, text, error = false) {
  node.replaceChildren(element("p", error ? "notice" : "muted", text));
}

function statistic(container, label, value) {
  const cell = element("div", "decision-stat");
  cell.append(element("span", "muted", label), element("strong", "mono", value));
  container.append(cell);
}

function chart(container, { points, xKey, xFormat, yFormat, series, band, floor, label }) {
  if (!points.length) return;
  const width = 650, height = 242;
  const left = 58, right = 19, top = 18, bottom = 209;
  const xValues = points.map(point => point[xKey]);
  const minX = Math.min(...xValues), maxX = Math.max(...xValues);
  const keys = [...series.map(line => line.key), ...(band ?? [])];
  const values = points.flatMap(point => keys.flatMap(key => point[key] == null ? [] : [Number(point[key])]));
  if (floor != null) values.push(Number(floor));
  if (!values.length) return;
  const minY = Math.min(...values), maxY = Math.max(...values);
  const padding = (maxY - minY || Math.max(100, Math.abs(maxY) / 10)) * .08;
  const low = minY - padding, high = maxY + padding;
  const x = value => left + (maxX === minX ? .5 : (value - minX) / (maxX - minX)) * (width - left - right);
  const y = value => bottom - (Number(value) - low) * (bottom - top) / (high - low);
  const drawing = svg("svg", { viewBox: "0 0 " + width + " " + height, role: "img", "aria-label": label });
  const marks = [];
  for (let index = 0; index < 5; index++) {
    const value = low + (high - low) * index / 4;
    const level = y(value);
    marks.push(svg("line", { x1: left, y1: level, x2: width - right, y2: level, class: "decision-grid-line" }));
    const tick = svg("text", { x: left - 7, y: level, "text-anchor": "end", "dominant-baseline": "middle" });
    tick.textContent = yFormat(value);
    marks.push(tick);
  }
  const ticks = [...new Set(Array.from({ length: 5 }, (_, index) =>
    Math.round((points.length - 1) * index / 4)))];
  for (const index of ticks) {
    const point = points[index];
    const node = svg("text", { x: x(point[xKey]), y: bottom + 22, "text-anchor": "middle" });
    node.textContent = xFormat(point[xKey]);
    marks.push(node);
  }
  if (band) {
    const highSide = points.map(point => x(point[xKey]) + " " + y(point[band[1]]));
    const lowSide = [...points].reverse().map(point => x(point[xKey]) + " " + y(point[band[0]]));
    marks.push(svg("path", { d: "M" + [...highSide, ...lowSide].join(" L") + " Z", class: "decision-band" }));
  }
  if (floor != null) marks.push(svg("line", {
    x1: left, x2: width - right, y1: y(floor), y2: y(floor), class: "decision-floor",
  }));
  for (const [index, line] of series.entries()) {
    let continuous = false;
    const parts = [];
    for (const point of points) {
      if (point[line.key] == null) { continuous = false; continue; }
      parts.push((continuous ? "L" : "M") + x(point[xKey]) + " " + y(point[line.key]));
      continuous = true;
      if (points.length <= 14) marks.push(svg("circle", {
        cx: x(point[xKey]), cy: y(point[line.key]), r: 2.6, class: "decision-dot decision-series-" + index,
      }));
    }
    marks.push(svg("path", { d: parts.join(" "), fill: "none", class: "decision-line decision-series-" + index }));
  }
  drawing.append(...marks);
  const legend = element("div", "decision-legend");
  series.forEach((line, index) => legend.append(element("span", "decision-key decision-series-" + index, line.label)));
  if (band) legend.append(element("span", "decision-key decision-key-band", "10th–90th percentile"));
  if (floor != null) legend.append(element("span", "decision-key decision-key-floor", "Cash reserve"));
  container.append(element("div", "decision-chart", drawing), legend);
}

function renderRisk(container, result, savedName) {
  container.replaceChildren();
  const forecast = panel(savedName ?? "Cash balance under uncertainty",
    "The model checks every daily closing balance for reserve breaches. The chart shows weekly balances and the selected purchase date.");
  const summary = element("div", "decision-stats");
  statistic(summary, "Paths meeting reserve every day", percent(result.success));
  statistic(summary, "Simulations", String(result.paths));
  statistic(summary, "10th-percentile low cash", money(result.low_cash));
  forecast.append(summary);
  const greatestDriver = result.drivers.find(driver => driver.improvement > 0);
  if (greatestDriver) forecast.append(element("p", "muted", greatestDriver.name + " drove the largest change in reserve success. Removing it improved success by " +
    Number(greatestDriver.improvement).toLocaleString(undefined, { maximumFractionDigits: 1 }) + " percentage points."));
  const dated = result.points.map(point => ({ ...point, time: Date.parse(point.date + "T00:00:00Z") }));
  chart(forecast, {
    points: dated, xKey: "time", xFormat: value => dateLabel(new Date(value).toISOString().slice(0, 10), { day: "numeric", month: "short" }),
    yFormat: value => compactMoney.format(value / 100), floor: result.reserve, band: ["p10", "p90"],
    series: [{ key: "p50", label: "Median" }, { key: "baseline", label: "Scheduled baseline" }], label: "Daily cash forecasts and cash reserve",
  });
  const dates = Object.entries(result.first_breaches);
  if (dates.length) {
    const peak = [...dates].sort((a, b) => b[1] - a[1])[0];
    forecast.append(element("p", "muted", "Most common first breach month: " +
      dateLabel(peak[0] + "-01", { month: "long", year: "numeric" }) + " · " + peak[1] + " paths"));
  }
  const values = element("details", "decision-values");
  values.append(element("summary", "", "Forecast values"),
    table(["Date", "10th percentile", "Median", "90th percentile", "Scheduled"],
      result.points.map(point => [dateLabel(point.date), money(point.p10), money(point.p50), money(point.p90), money(point.baseline)])));
  forecast.append(values);

  const reverse = panel("Reverse planner", "Uses a " + result.confidence + "% reserve success target across the full modeled horizon.");
  $("h2", reverse).dataset.featureHelp = "reverse";
  const measures = element("div", "decision-stats");
  statistic(measures, "Purchase capacity on " + dateLabel(result.purchase_on),
    result.capacity == null ? "Target already missed" : money(result.capacity));
  statistic(measures, "Earliest date for " + money(result.purchase_amount), safeDate(result.earliest_purchase));
  reverse.append(measures);
  if (!result.earliest_purchase) reverse.append(element("p", "muted", "No date meets the target within this forecast."));
  if (result.capacity_points) chart(reverse, {
    points: result.capacity_points.map(point => ({ ...point, time: Date.parse(point.date + "T00:00:00Z") })),
    xKey: "time", xFormat: value => dateLabel(new Date(value).toISOString().slice(0, 10), { day: "numeric", month: "short" }),
    yFormat: value => compactMoney.format(value / 100),
    series: [{ key: "capacity", label: "Maximum purchase at target success" }], label: "Purchase capacity across forecast dates",
  });

  const stress = result.stress;
  const limits = panel("Reverse stress test", "Changes one assumption at a time on the scheduled baseline.");
  if (stress.baseline_breach) limits.append(element("p", "notice", "The scheduled baseline first falls short on " + dateLabel(stress.baseline_breach) + "."));
  else {
    const entries = [["Extra expense on purchase day", money(stress.extra_expense)],
      ["Cut in scheduled income", stress.income_cut_pct == null ? "No breach even at 100%" : percent(stress.income_cut_pct)]];
    if (stress.reimbursement) entries.push([
      stress.reimbursement.name + " · " + dateLabel(stress.reimbursement.due_on),
      stress.reimbursement.first_failing_delay == null ? "No breach when omitted within horizon"
        : stress.reimbursement.first_failing_delay + " days late",
    ]);
    limits.append(table(["Change", "First value that causes a breach"], entries));
  }

  const drivers = panel("Risk driver ranking", "Shows the change in reserve success if each source of uncertainty is removed on the same simulated paths. Effects can overlap.");
  drivers.append(table(["Source", "Change in success"], result.drivers.map(driver => [
    driver.name, (driver.improvement > 0 ? "+" : "") + Number(driver.improvement).toLocaleString(undefined, { maximumFractionDigits: 1 }) + " points",
  ])));
  if (result.history_months) drivers.append(element("p", "muted", result.history_months + " completed historical months sampled."));
  container.append(forecast, reverse, limits, drivers);
}

function goalFields(container, setup, optional = false) {
  const suggestions = setup.goals.map(goal => [goal.id, goal.name]);
  const from = field(container, "from_id", "Goal giving up funding", "", { items: [["", optional ? "No goal reallocation" : "Choose goal"], ...suggestions] });
  const to = field(container, "to_id", "Goal receiving funding", "", { items: [["", "Choose goal"], ...suggestions] });
  const account = field(container, "account_id", "Shared savings account", "", { items: [["", "Choose linked account"]] });
  function sync() {
    const left = setup.goals.find(goal => String(goal.id) === from.value);
    const right = setup.goals.find(goal => String(goal.id) === to.value);
    const common = setup.accounts.filter(item => left?.accounts.includes(item.id) && right?.accounts.includes(item.id));
    options(account, [["", "Choose linked account"], ...common.map(item => [item.id, item.name])],
      common.some(item => String(item.id) === account.value) ? account.value : common[0]?.id ?? "");
    to.disabled = !from.value;
    account.disabled = !from.value;
  }
  from.addEventListener("change", () => { if (!from.value) to.value = ""; sync(); });
  to.addEventListener("change", sync);
  sync();
}

export function setupDecision(root, { notify = () => {} } = {}) {
  const heading = element("div", "page-heading");
  const title = element("div");
  title.append(element("h1", "", "Decision lab"),
    element("p", "page-description", "Test cash reserves, compare trade-offs, and review forecasts against recorded balances."));
  heading.append(title);
  const tabs = element("div", "segmented decision-tabs");
  tabs.setAttribute("role", "group");
  tabs.setAttribute("aria-label", "Decision lab tool");
  const buttons = {};
  const view = element("div", "decision-view");
  root.append(heading, tabs, view);
  let setup, assumptions, current = "risk", revision = 0;
  for (const [mode, name] of Object.entries(modes)) {
    buttons[mode] = button(name, "");
    buttons[mode].addEventListener("click", () => open(mode));
    tabs.append(buttons[mode]);
  }

  async function open(mode = current) {
    current = mode;
    root.dataset.helpContext = mode;
    for (const [name, control] of Object.entries(buttons)) control.setAttribute("aria-pressed", String(name === mode));
    const token = ++revision;
    message(view, "Loading decision lab…");
    try {
      setup = await api(base + "setup");
      if (token !== revision) return;
      if (!assumptions) {
        const day = new Date(setup.today + "T00:00:00Z");
        day.setUTCDate(day.getUTCDate() + 30);
        assumptions = {
          days: 180, paths: 350, seed: 42, reserve: "5000", confidence: 90, purchase_amount: "0",
          purchase_on: day.toISOString().slice(0, 10), income_pct: 5, income_miss_pct: 0, expense_pct: 8,
          delay_pct: 25, delay_days: 21, shock_pct: 20, shock_amount: "300", history: false,
        };
      }
      view.replaceChildren();
      if (mode === "risk") riskView(token);
      if (mode === "optimize") optimizeView(token);
      if (mode === "tradeoff") tradeoffView(token);
      if (mode === "score") await scoreView(token);
    } catch (error) {
      if (token === revision) message(view, apiMessage(error), true);
    }
  }

  function riskView(token) {
    const controls = panel("Model assumptions",
      "The modeled purchase is added to scheduled commitments. Set how income, bills, reimbursements, and surprise expenses can vary.");
    const form = element("form");
    const main = element("div", "field-grid");
    const advanced = element("details", "decision-values");
    advanced.append(element("summary", "", "Advanced assumptions"));
    const grid = element("div", "field-grid");
    const numeric = new Set();
    const measured = (parent, key, label, min, max) => {
      numeric.add(key);
      return field(parent, key, label, assumptions[key], { type: "number", min, max, required: true });
    };
    const amount = (parent, key, label) => field(parent, key, label, assumptions[key], { inputMode: "decimal", required: true });
    const duration = measured(grid, "days", "Horizon in days", 30, 730);
    measured(grid, "confidence", "Target success %", 1, 100);
    amount(main, "reserve", "Cash reserve (€)");
    amount(main, "purchase_amount", "Purchase amount (€)");
    const purchase = field(main, "purchase_on", "Purchase date", assumptions.purchase_on,
      { type: "date", min: setup.today, required: true });
    const updateMax = () => {
      if (!duration.value) return;
      const end = new Date(setup.today + "T00:00:00Z");
      end.setUTCDate(end.getUTCDate() + Number(duration.value));
      purchase.max = end.toISOString().slice(0, 10);
    };
    duration.addEventListener("input", updateMax);
    updateMax();
    form.addEventListener("invalid", event => {
      if (grid.contains(event.target)) advanced.open = true;
    }, true);
    measured(grid, "paths", "Simulation paths", 100, 1000);
    measured(grid, "seed", "Random seed", 0, 4294967295);
    measured(grid, "income_pct", "Income variation ±%", 0, 100);
    measured(grid, "income_miss_pct", "Chance of missing a scheduled income %", 0, 100);
    measured(grid, "expense_pct", "Bill variation ±%", 0, 100);
    measured(grid, "delay_pct", "Chance of a late scheduled reimbursement %", 0, 100);
    measured(grid, "delay_days", "Maximum delay in days", 1, 90);
    measured(grid, "shock_pct", "Chance of a surprise expense per month %", 0, 100);
    amount(grid, "shock_amount", "Surprise expense (€)");
    field(grid, "history", "Sample unscheduled activity from completed months", assumptions.history, { type: "checkbox" });
    advanced.append(grid, element("p", "muted", "Surprise-expense chances are prorated for partial months. Historical sampling needs three completed months and omits known recurring items and shared activity."));
    const actions = element("div", "decision-actions");
    const run = button("Run model", "primary");
    run.type = "submit";
    const name = field(actions, "forecast_name", "Forecast name", "", { placeholder: "e.g. Replace the car" });
    const save = button("Save forecast", "secondary");
    const saved = panel("Saved risk forecasts");
    const picker = field(saved, "saved_run", "Open forecast", "", { items: [["", "Choose saved forecast"]] });
    const results = element("div", "decision-results");
    const status = element("div");
    if (!setup.accounts.length) message(status, "Add a cash account before running the model.");
    run.disabled = save.disabled = !setup.accounts.length;
    actions.append(run, save);
    form.append(main, advanced, actions, status);
    controls.append(form);
    view.append(controls, results, saved);
    let request = 0;
    function formAssumptions() {
      const values = Object.fromEntries(new FormData(form));
      delete values.forecast_name;
      for (const key of numeric) values[key] = Number(values[key]);
      values.history = Boolean(values.history);
      return values;
    }
    form.addEventListener("input", event => {
      if (event.target !== name) assumptions = formAssumptions();
    });
    async function loadSaved() {
      try {
        const records = await api(base + "runs");
        if (revision === token) options(picker, records.map(record => [record.id, record.name + " · " + dateLabel(record.issued_on)]),
          picker.value, "Choose saved forecast");
      } catch (error) { if (revision === token) message(status, apiMessage(error), true); }
    }
    async function calculate(saving) {
      if (!form.reportValidity() || !setup.accounts.length) return;
      if (saving && !name.value.trim()) { message(status, "Enter a name for this forecast.", true); name.focus(); return; }
      assumptions = formAssumptions();
      const selected = ++request;
      run.disabled = save.disabled = true;
      message(status, saving ? "Calculating and saving forecast…" : "Simulating daily balances…");
      try {
        const response = await api(base + (saving ? "runs" : "analyze"), {
          method: "POST", data: saving ? { name: name.value, assumptions } : assumptions,
        });
        if (revision !== token || selected !== request) return;
        renderRisk(results, saving ? response.result : response, saving ? response.name : undefined);
        status.replaceChildren();
        if (saving) { notify("Forecast saved."); loadSaved(); }
      } catch (error) {
        if (revision === token && selected === request) message(status, apiMessage(error), true);
      } finally {
        if (selected === request) run.disabled = save.disabled = false;
      }
    }
    form.addEventListener("submit", event => { event.preventDefault(); calculate(false); });
    save.addEventListener("click", () => calculate(true));
    picker.addEventListener("change", async () => {
      if (!picker.value) return;
      const selected = ++request;
      try {
        const record = await api(base + "runs/" + picker.value);
        if (revision === token && request === selected) {
          status.replaceChildren();
          renderRisk(results, record.result, record.name + " · saved " + dateLabel(record.issued_on));
        }
      } catch (error) { if (revision === token) message(status, apiMessage(error), true); }
      finally { if (selected === request) run.disabled = save.disabled = !setup.accounts.length; }
    });
    loadSaved();
  }

  function optimizeView(token) {
    const area = panel("Compare possible decisions",
      "Uses the assumptions in Risk simulator. Investment reductions release scheduled contributions, and goal reallocations change how savings are earmarked.");
    const defaults = element("p", "muted", "Purchase " + moneyFromInput(assumptions.purchase_amount) + " on "
      + safeDate(assumptions.purchase_on) + " · reserve " + moneyFromInput(assumptions.reserve)
      + " · target " + percent(assumptions.confidence));
    area.append(defaults);
    const form = element("form", "decision-form");
    const grid = element("div", "field-grid");
    field(grid, "delay_days", "Latest delay to test in days", "60", { type: "number", min: 0, max: 180, step: 1, required: true });
    field(grid, "investment_flex", "Monthly investment flexibility (€)", "100", { inputMode: "decimal", required: true });
    if (setup.goals.length > 1) {
      goalFields(grid, setup, true);
      field(grid, "funding_account", "Account for purchase and added investing", "", {
        items: [["", "Choose if comparing goals"], ...setup.accounts.map(item => [item.id, item.name])],
      });
    }
    const submit = button("Find trade-offs", "primary");
    submit.type = "submit";
    submit.disabled = !setup.accounts.length;
    const results = element("div", "decision-results");
    form.append(grid, submit);
    area.append(form);
    view.append(area, results);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const inputs = Object.fromEntries(new FormData(form));
      inputs.delay_days = Number(inputs.delay_days);
      submit.disabled = true;
      message(results, "Evaluating decisions on common simulated paths…");
      try {
        const data = await api(base + "optimize", { method: "POST", data: { ...inputs, assumptions } });
        if (revision !== token) return;
        const headline = data.qualified ? data.qualified + " of " + data.candidates + " candidates met the "
          + data.confidence + "% target." : "No candidate met the target. The table shows the remaining trade-offs.";
        const report = panel("Decision trade-offs", headline);
        report.append(element("p", "muted", "Each row is a trade-off among cash safety, purchase timing, and investment contributions. When goals are selected, dates show when they first reach their funding targets."));
        report.append(table(["Purchase date", "Monthly investment change", "Reserve success", "10th-percentile low cash",
          ...(data.goal_names.length ? ["Monthly redirect", ...data.goal_names] : [])], data.frontier.map(row => [
          dateLabel(row.purchase_on), signedMoney(row.investment_change), percent(row.success), money(row.low_cash),
          ...(data.goal_names.length ? [money(row.redirect), safeDate(row.from_on), safeDate(row.to_on)] : []),
        ])));
        results.replaceChildren(report);
      } catch (error) { if (revision === token) message(results, apiMessage(error), true); }
      finally { submit.disabled = false; }
    });
  }

  function tradeoffView(token) {
    const area = panel("Goal trade-off curve",
      "Redirect part of one goal’s planned monthly contribution to another goal linked to the same savings account.");
    if (setup.goals.length < 2) {
      area.append(element("p", "muted", "Create two goals with a common funding account to compare them."));
      view.append(area);
      return;
    }
    const form = element("form", "decision-form"), grid = element("div", "field-grid");
    goalFields(grid, setup);
    const submit = button("Plot completion dates", "primary");
    submit.type = "submit";
    const report = element("div", "decision-results");
    form.append(grid, submit);
    area.append(form);
    view.append(area, report);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      submit.disabled = true;
      message(report, "Calculating goal completion dates…");
      try {
        const data = await api(base + "tradeoff", { method: "POST", data: Object.fromEntries(new FormData(form)) });
        if (revision !== token) return;
        const result = panel("Estimated completion", "Amounts are monthly redirects. Missing dates mean a goal did not complete within the model’s " + data.horizon_months + "-month horizon.");
        const days = date => date ? (Date.parse(date + "T00:00:00Z") - Date.parse(data.today + "T00:00:00Z")) / 86400000 : null;
        const points = data.points.map(point => ({ x: Number(point.redirect), source: days(point.from_on), target: days(point.to_on) }));
        chart(result, { points, xKey: "x", xFormat: value => compactMoney.format(value / 100),
          yFormat: value => Math.max(0, Math.round(value / 30.4375)) + " mo", label: "Goal completion estimates by monthly redirect",
          series: [{ key: "source", label: data.source }, { key: "target", label: data.target }] });
        result.append(table(["Monthly redirect", data.source, data.target], data.points.map(point => [
          money(point.redirect), safeDate(point.from_on), safeDate(point.to_on),
        ])));
        report.replaceChildren(result);
      } catch (error) { if (revision === token) message(report, apiMessage(error), true); }
      finally { submit.disabled = false; }
    });
  }

  async function scoreView(token) {
    const report = panel("Forecast scorecard",
      "Compares completed dates with forecasts saved before them. Positive error means actual cash exceeded the prediction. Band coverage uses saved risk forecasts.");
    view.append(report);
    const data = await api(base + "scorecard");
    if (revision !== token) return;
    const stats = element("div", "decision-stats");
    statistic(stats, "Observed checkpoints", String(data.overall.count));
    statistic(stats, "Within forecast band", percent(data.overall.coverage));
    statistic(stats, "Average absolute error", money(data.overall.mae));
    statistic(stats, "Average signed error", signedMoney(data.overall.bias));
    report.append(stats);
    if (data.runs.length) {
      const labels = { observed: "Observed", awaiting_observations: "Awaiting completed dates", account_scope_unavailable: "Original account scope unavailable",
        no_projection: "No feasible projection" };
      const kinds = { risk: "Risk model", "what-if": "What-if baseline", purchase: "Purchase plan" };
      report.append(table(["Forecast", "Model", "Saved", "Status", "Checkpoints", "Band coverage", "Absolute error", "Signed error"],
        data.runs.map(run => [run.name, kinds[run.source], dateLabel(run.issued_on), labels[run.status], run.count,
          percent(run.coverage), money(run.mae), signedMoney(run.bias)])));
    } else report.append(element("p", "muted", "Save a risk forecast to start tracking its accuracy."));
  }

  return { open };
}

function moneyFromInput(value) {
  return "€" + String(value);
}
