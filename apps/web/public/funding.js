import { api, apiMessage, button, dateLabel, element, euros, icon, money, options, svg } from "./ui.js";

const endpoint = "/api/funding/";
const defaultDays = 60;
const maxDays = 366;
const dayMs = 86_400_000;
const shortDate = value => dateLabel(value, { day: "numeric", month: "short" });
const dateOrDash = value => value ? dateLabel(value) : "—";
const exact = value => money(value, { fixed: true });
const compact = new Intl.NumberFormat("en", { style: "currency", currency: "EUR", notation: "compact", maximumFractionDigits: 1 });

function panel(title, description) {
  const node = element("section", "panel funding-panel");
  node.append(element("h2", "", title));
  if (description) node.append(element("p", "muted", description));
  return node;
}

function field(container, name, label, attrs = {}) {
  const { items, value, prompt, ...properties } = attrs;
  const node = element(items ? "select" : "input");
  node.name = name;
  if (items) options(node, items, value, prompt);
  else node.value = value ?? "";
  Object.assign(node, properties);
  const wrap = element("label", "field");
  wrap.append(element("span", "", label), node);
  container.append(wrap);
  return node;
}

function submit(text, className = "primary") {
  const node = button(text, className);
  node.type = "submit";
  return node;
}

function feedback() {
  const node = element("p", "notice funding-feedback");
  node.setAttribute("role", "alert");
  node.hidden = true;
  return node;
}

function issue(node, error) {
  node.textContent = apiMessage(error);
  node.hidden = false;
}

function accountLabel(account) {
  if (!account) return "Account";
  return account.profile_name ? account.name + " · " + account.profile_name : account.name;
}

function displayName(accounts, id) {
  return accountLabel(accounts.get(String(id)));
}

function facts(values, className = "funding-facts") {
  const node = element("dl", className);
  for (const [label, value] of values) {
    const cell = element("div");
    cell.append(element("dt", "muted", label), element("dd", "mono", value));
    node.append(cell);
  }
  return node;
}

function table(headers, rows) {
  const node = element("table");
  const heading = element("tr");
  for (const value of headers) {
    const cell = element("th", "", value);
    cell.scope = "col";
    heading.append(cell);
  }
  const body = element("tbody");
  for (const values of rows) {
    const row = element("tr");
    row.append(...values.map(value => element("td", "", value)));
    body.append(row);
  }
  node.append(element("thead", "", heading), body);
  return element("div", "table-scroll", node);
}

function stateBadge(scenario) {
  const text = scenario.safe ? "Covered as scheduled" : scenario.can_fund ? "Covered with transfers" : "Shortfall remains";
  const tone = scenario.safe ? "funded" : scenario.can_fund ? "transfer" : "problem";
  return element("span", "funding-state funding-" + tone, text);
}

function shortfallBlock(shortfall, title) {
  const box = element("div", "funding-shortfall");
  box.append(element("h3", "", title),
    element("p", "", dateLabel(shortfall.date) + " · " + exact(shortfall.amount) + " short"),
    facts([["Projected balance", exact(shortfall.balance)], ["Minimum balance", exact(shortfall.floor)]]));
  if (shortfall.events.length) {
    const costs = element("details", "funding-inspect");
    costs.append(element("summary", "", "Cash movements affecting this issue"),
      table(["Date", "Cash movement", "Amount"], shortfall.events.map(event => [
        dateOrDash(event.date), event.name, exact(event.amount),
      ])));
    box.append(costs);
  }
  return box;
}

function transferPlan(scenario, accounts, blockedUntil) {
  if (blockedUntil) return element("p", "muted", "The payment account becomes available on " + dateLabel(blockedUntil) + ".");
  if (scenario.safe) return element("p", "muted", "No new transfer is needed.");
  if (!scenario.transfers.length) {
    return element("p", "muted", "The recorded routes and available balances cannot cover this shortfall.");
  }
  const section = element("div", "funding-transfer-plan");
  section.append(facts([[scenario.can_fund ? "Smallest transfer total" : "Transfers considered", exact(scenario.moved)]]),
    table(["From", "Into", "Send on", "Arrives on", "Amount"], scenario.transfers.map(transfer => [
      displayName(accounts, transfer.source_id), displayName(accounts, transfer.destination_id),
      dateLabel(transfer.sent_on), dateLabel(transfer.arrives_on), exact(transfer.amount),
    ])));
  return section;
}

function dailyChart(points) {
  const result = element("details", "funding-inspect funding-daily");
  result.append(element("summary", "", "Inspect daily balances"));
  const lines = [
    { key: "balance", label: "Without new transfers", style: "funding-line-before" },
    { key: "funded_balance", label: "With suggested transfers", style: "funding-line-after" },
    { key: "floor", label: "Minimum balance", style: "funding-line-floor" },
  ].filter(line => line.key !== "funded_balance" || points.some(point => point.funded_balance != null && point.funded_balance !== point.balance));
  const width = 620, height = 190;
  const left = 55, right = 20, top = 16, bottom = 154;
  const series = points.flatMap(point => lines.map(({ key }) => Number(point[key])));
  const min = Math.min(...series), max = Math.max(...series);
  const padding = Math.max(100, (max - min) / 12);
  const low = min - padding, high = max + padding;
  const x = index => left + (points.length === 1 ? .5 : index / (points.length - 1)) * (width - left - right);
  const y = value => bottom - (Number(value) - low) / (high - low) * (bottom - top);
  const drawing = svg("svg", {
    viewBox: "0 0 " + width + " " + height, role: "img", "aria-label": "Projected payment account balances and minimum balance by date",
  });
  for (let index = 0; index < 4; index++) {
    const value = low + (high - low) * index / 3;
    const line = y(value);
    drawing.append(svg("line", { x1: left, x2: width - right, y1: line, y2: line, class: "funding-grid-line" }));
    const tick = svg("text", { x: left - 7, y: line, "text-anchor": "end", "dominant-baseline": "middle" });
    tick.textContent = compact.format(value / 100);
    drawing.append(tick);
  }
  const ticks = new Set(Array.from({ length: 5 }, (_, index) => Math.round((points.length - 1) * index / 4)));
  for (const index of ticks) {
    const tick = svg("text", { x: x(index), y: bottom + 20, "text-anchor": "middle" });
    tick.textContent = shortDate(points[index].date);
    drawing.append(tick);
  }
  for (const { key, style } of lines) {
    const coords = points.map((point, index) => x(index) + "," + y(point[key])).join(" ");
    drawing.append(svg("polyline", { points: coords, class: "funding-chart-line " + style, fill: "none" }));
    if (points.length === 1) drawing.append(svg("circle", { cx: x(0), cy: y(points[0][key]), r: 3, class: style }));
  }
  const ruler = svg("line", { x1: x(0), x2: x(0), y1: top, y2: bottom, class: "funding-chart-cursor" });
  drawing.append(ruler);
  const chart = element("div", "funding-chart", drawing);
  const legend = element("div", "funding-legend");
  for (const { label, style } of lines) legend.append(element("span", style, label));
  const inspection = element("div", "funding-day-inspect");
  const picker = field(inspection, "day", "Day to inspect", {
    items: points.map(point => [point.date, dateLabel(point.date)]), value: points[0].date,
  });
  const selected = element("div");
  selected.setAttribute("aria-live", "polite");
  function showDay() {
    const index = points.findIndex(point => point.date === picker.value);
    if (index < 0) return;
    const point = points[index];
    ruler.setAttribute("x1", x(index));
    ruler.setAttribute("x2", x(index));
    selected.replaceChildren(facts(lines.map(({ key, label }) => [label, exact(point[key])])));
  }
  picker.addEventListener("change", showDay);
  inspection.append(selected);
  showDay();
  result.append(chart, legend, inspection);
  return result;
}

function outcome(result, scenario, title, accounts) {
  const section = panel(title);
  const header = element("div", "funding-outcome-title");
  header.append(section.firstChild, stateBadge(scenario));
  section.prepend(header);
  const existingIssue = scenario.baseline_first_shortfall;
  const proposedIssue = scenario.first_shortfall;
  if (proposedIssue) {
    section.append(shortfallBlock(proposedIssue, "First issue before new transfers"));
  }
  if (existingIssue && (!proposedIssue || existingIssue.date !== proposedIssue.date || existingIssue.amount !== proposedIssue.amount)) {
    section.append(shortfallBlock(existingIssue, "First issue from existing obligations"));
  }
  section.append(transferPlan(scenario, accounts, result.blocked_until));
  if (BigInt(result.amount) > 0n) {
    const timing = [["Earliest payment date without new transfers",
      scenario.earliest_on ? dateLabel(scenario.earliest_on) : "No date found in this forecast"],
    ];
    if (!scenario.can_fund && scenario.earliest_funded_on && scenario.earliest_funded_on !== scenario.earliest_on) {
      timing.push(["Earliest with an approved transfer", dateLabel(scenario.earliest_funded_on)]);
    }
    section.append(facts(timing));
  }
  if (scenario.points.length) section.append(dailyChart(scenario.points));
  return section;
}

export function setupFunding(root, { notify = () => {}, onChange = () => {} } = {}) {
  let setup, form, draft, result, opening, checking, pendingPolicy = false;
  let policiesOpen = false;
  const heading = element("div", "page-heading");
  const headingCopy = element("div");
  headingCopy.append(element("h1", "", "Funding check"),
    element("p", "page-description", "See when a payment account falls short and which transfers can arrive in time."));
  heading.append(headingCopy);
  const loadError = feedback();
  const view = element("div", "funding-view");
  const resultBox = element("div", "funding-results");
  resultBox.setAttribute("aria-live", "polite");
  const policyBox = element("div", "funding-policies-container");
  root.replaceChildren(heading, loadError, view, resultBox, policyBox);

  async function load({ rerender = true, signal } = {}) {
    const data = await api(endpoint + "setup", { signal });
    if (signal?.aborted) return;
    setup = data;
    if (rerender) render();
  }

  async function open(prefill = {}) {
    opening?.abort();
    checking?.abort();
    const request = new AbortController();
    opening = request;
    loadError.hidden = true;
    draft = {
      account_id: prefill.account_id ?? "", amount: prefill.amount ?? "0",
      purchase_on: prefill.purchase_on ?? "", days: defaultDays, reimbursement_delay: 7,
      label: prefill.label ?? "",
    };
    result = null;
    resultBox.replaceChildren();
    policyBox.replaceChildren();
    view.replaceChildren(element("p", "muted", "Loading funding settings…"));
    try {
      await load({ signal: request.signal });
    } catch (error) {
      if (!request.signal.aborted) {
        issue(loadError, error);
        const retry = button("Retry");
        retry.addEventListener("click", () => open(prefill));
        view.replaceChildren(retry);
      }
    }
  }

  function adjustDays() {
    const days = 1 + Math.floor((Date.parse(draft.purchase_on) - Date.parse(setup.today)) / dayMs);
    if (days > 0 && days <= maxDays && Number(draft.days) < days) draft.days = days;
  }

  function render() {
    const accounts = setup.accounts.filter(account => account.kind !== "investment" && account.opened_on <= setup.today)
      .map(account => [account.id, accountLabel(account)]);
    if (!accounts.length) {
      const add = element("a", "secondary", "Open accounts");
      add.href = "#accounts";
      const empty = panel("Add an account", "A funding check needs a payment account from your ledger.");
      empty.append(add);
      view.replaceChildren(empty);
      if (setup.accounts.length) policyBox.replaceChildren(renderPolicies());
      return;
    }
    const accountIds = new Set(accounts.map(([key]) => String(key)));
    if (!accountIds.has(String(draft.account_id))) draft.account_id = accounts[0][0];
    if (!draft.purchase_on || draft.purchase_on < setup.today) draft.purchase_on = setup.today;
    adjustDays();
    const box = panel("What needs funding?", draft.label ? draft.label : "Leave the amount at zero to check bills already in the forecast.");
    const error = feedback();
    form = element("form", "funding-form");
    const fields = element("div", "field-grid funding-check-fields");
    const account = field(fields, "account_id", "Pay from", { items: accounts, value: draft.account_id, required: true });
    const amount = field(fields, "amount", "Additional payment (€)", { inputMode: "decimal", value: draft.amount, required: true });
    const date = field(fields, "purchase_on", "Payment date", {
      type: "date", value: draft.purchase_on, min: setup.today, required: true,
    });
    const days = field(fields, "days", "Forecast days", {
      type: "number", min: 1, max: maxDays, step: 1, value: draft.days, required: true,
    });
    const delay = field(fields, "reimbursement_delay", "Reimbursement delay (days)", {
      type: "number", min: 0, max: 90, step: 1, value: draft.reimbursement_delay, required: true,
    });
    for (const control of [account, amount, date, days, delay]) {
      control.addEventListener("input", () => {
        draft[control.name] = control.value;
        result = null;
        resultBox.replaceChildren();
        error.hidden = true;
      });
    }
    date.addEventListener("change", () => { adjustDays(); days.value = draft.days; });
    const actions = element("div", "funding-actions");
    const run = submit("Check funding");
    actions.append(run);
    if (setup.example) {
      const sample = button("Load sample purchase");
      sample.addEventListener("click", () => {
        checking?.abort();
        Object.assign(draft, setup.example);
        result = null;
        resultBox.replaceChildren();
        render();
        form.querySelector("button[type='submit']").focus();
      });
      actions.append(sample);
    }
    form.append(fields, actions);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (run.disabled) return;
      checking?.abort();
      const request = new AbortController();
      checking = request;
      const payload = {
        account_id: account.value, amount: amount.value, purchase_on: date.value, days: Number(days.value),
        reimbursement_delay: Number(delay.value), label: draft.label.slice(0, 100),
      };
      error.hidden = true;
      for (const control of [account, amount, date, days, delay, run]) control.disabled = true;
      resultBox.replaceChildren(element("p", "muted", "Checking account balances and transfer dates…"));
      try {
        const next = await api(endpoint + "check", { method: "POST", data: payload, signal: request.signal });
        if (request.signal.aborted) return;
        result = next;
        renderResult();
      } catch (problem) {
        if (!request.signal.aborted) {
          resultBox.replaceChildren();
          issue(error, problem);
        }
      } finally {
        if (!request.signal.aborted) for (const control of [account, amount, date, days, delay, run]) control.disabled = false;
      }
    });
    box.append(error, form);
    view.replaceChildren(box);
    policyBox.replaceChildren(renderPolicies());
    if (result) renderResult();
  }

  function renderResult() {
    const accounts = new Map(setup.accounts.map(account => [String(account.id), account]));
    const heading = panel("Funding result",
      [displayName(accounts, result.account_id), "Through " + dateLabel(result.through)].join(" · "));
    const title = result.label || (BigInt(result.amount) > 0n ? "Additional payment" : "Scheduled bills");
    heading.append(facts([[title, BigInt(result.amount) > 0n ? exact(result.amount) : dateLabel(result.purchase_on)]]));
    if (BigInt(result.amount) > 0n) heading.append(element("p", "muted", "Payment on " + dateLabel(result.purchase_on)));
    const base = outcome(result, result.base, "Expected schedule", accounts);
    const parts = [heading, base];
    if (result.delayed_reimbursements.length) {
      const stress = outcome(result, result.late, "Reimbursements delayed by " + result.reimbursement_delay + " days", accounts);
      const detail = element("details", "funding-inspect");
      detail.append(element("summary", "", "Reimbursements in this scenario"),
        table(["Reimbursement", "Scheduled", "Arrives", "Amount"], result.delayed_reimbursements.map(item => [
          item.name, dateLabel(item.scheduled_on), dateLabel(item.arrives_on), exact(item.amount),
        ])));
      stress.append(detail);
      parts.push(stress);
    } else if (result.reimbursement_delay) {
      parts.push(element("p", "muted", "There are no scheduled reimbursements to delay in this forecast."));
    }
    resultBox.replaceChildren(...parts);
  }

  async function updatePolicy(method, path, data, output, controls, message) {
    if (pendingPolicy) return;
    output.hidden = true;
    pendingPolicy = true;
    controls.forEach(control => { control.disabled = true; });
    // A result from older availability or transfer settings no longer reflects the saved policy.
    try {
      await api(endpoint + path, { method, data });
      checking?.abort();
      result = null;
      resultBox.replaceChildren();
      notify(message);
      onChange();
      await load();
    } catch (error) {
      issue(output, error);
    } finally {
      pendingPolicy = false;
      controls.forEach(control => { control.disabled = false; });
    }
  }

  function accountPolicy(account) {
    const row = element("article", "funding-policy-item");
    const title = element("div", "funding-policy-title");
    title.append(icon(account.kind === "savings" ? "savings" : "wallet"),
      element("h3", "", accountLabel(account)));
    row.append(title, facts([
      ["Minimum balance", exact(account.reserve)], ["Protected funds", exact(account.protected)],
      ["Available from", account.available_on ? dateLabel(account.available_on)
        : account.kind === "investment" ? "Set a maturity date" : "Now"],
    ]));
    if (!account.can_edit) {
      row.append(element("p", "muted", "Policy managed by the account owner."));
      return row;
    }
    const toggle = element("details", "funding-inspect");
    toggle.append(element("summary", "", "Edit account policy"));
    const form = element("form", "funding-form");
    const grid = element("div", "field-grid");
    const reserve = field(grid, "reserve", "Minimum balance (€)", { value: euros(account.reserve), inputMode: "decimal", required: true });
    const available = field(grid, "available_on", account.kind === "investment" ? "Maturity date" : "Available from (optional)",
      { type: "date", value: account.available_on, min: account.opened_on });
    const save = submit("Save policy", "secondary");
    const error = feedback();
    form.append(grid, error, element("div", "funding-actions", save));
    form.addEventListener("submit", event => {
      event.preventDefault();
      updatePolicy("PUT", "accounts/" + encodeURIComponent(account.id),
        { reserve: reserve.value, available_on: available.value || null }, error,
        [save, reserve, available], "Account policy saved.");
    });
    toggle.append(form);
    row.append(toggle);
    return row;
  }

  function routePolicy() {
    const section = element("div", "funding-route-policy");
    section.append(element("h3", "", "Transfer routes"),
      element("p", "muted", "Set how many business days a transfer takes. A route starts from an account you can manage."));
    const accounts = new Map(setup.accounts.map(account => [String(account.id), account]));
    if (setup.routes.length) {
      const rows = setup.routes.map(route => {
        const actions = element("div", "funding-route-action");
        if (route.can_edit) {
          const remove = button("Remove", "text-button", "", "Remove route from " + displayName(accounts, route.source_id)
            + " to " + displayName(accounts, route.destination_id));
          const error = feedback();
          remove.addEventListener("click", () => updatePolicy("DELETE", "routes/" + encodeURIComponent(route.id),
            undefined, error, [remove], "Transfer route removed."));
          if (route.active !== false) {
            const edit = element("details", "funding-inspect");
            const form = element("form", "funding-actions");
            const days = field(form, "days", "Business days", {
              type: "number", min: 0, max: 14, step: 1, required: true, value: route.days,
            });
            const save = submit("Save delay", "secondary");
            form.append(save);
            form.addEventListener("submit", event => {
              event.preventDefault();
              updatePolicy("PUT", "routes/" + encodeURIComponent(route.id), { days: Number(days.value) },
                error, [save, days], "Transfer delay updated.");
            });
            edit.append(element("summary", "", "Edit delay"), form);
            actions.append(edit);
          }
          actions.append(remove, error);
        } else actions.append(element("span", "muted", "Owner managed"));
        if (route.active === false) actions.append(element("p", "muted", "Inactive · source maturity needed"));
        const delay = Number(route.days) === 0 ? "Same day" : route.days + (Number(route.days) === 1 ? " business day" : " business days");
        return [displayName(accounts, route.source_id), displayName(accounts, route.destination_id), delay, actions];
      });
      section.append(table(["Source", "Destination", "Delivery", ""], rows));
    } else section.append(element("p", "muted", "There are no explicit transfer routes."));
    const destinations = setup.accounts.filter(account => account.kind !== "investment");
    const sources = setup.accounts.filter(account => account.can_edit && (account.kind !== "investment" || account.available_on)
      && destinations.some(destination => String(destination.id) !== String(account.id)));
    if (!sources.length) return section;
    const form = element("form", "funding-form");
    const grid = element("div", "field-grid funding-route-fields");
    const source = field(grid, "source_id", "Source account", { items: sources.map(account => [account.id, accountLabel(account)]), required: true });
    const destination = field(grid, "destination_id", "Destination account", { items: [], required: true });
    const duration = field(grid, "days", "Business days", { type: "number", value: 0, min: 0, max: 14, step: 1, required: true });
    const updateDestinations = () => options(destination, destinations
      .filter(account => String(account.id) !== source.value).map(account => [account.id, accountLabel(account)]));
    source.addEventListener("change", updateDestinations);
    updateDestinations();
    const save = submit("Add transfer route", "secondary");
    const error = feedback();
    form.append(grid, error, element("div", "funding-actions", save));
    form.addEventListener("submit", event => {
      event.preventDefault();
      updatePolicy("POST", "routes", {
        source_id: source.value, destination_id: destination.value, days: Number(duration.value),
      }, error, [save, source, destination, duration], "Transfer route saved.");
    });
    section.append(form);
    return section;
  }

  function renderPolicies() {
    const details = element("details", "panel funding-panel funding-policies");
    details.open = policiesOpen;
    details.addEventListener("toggle", () => { if (details.isConnected) policiesOpen = details.open; });
    const title = element("summary", "", "Account policies and transfer routes");
    const items = element("div", "funding-policy-list");
    for (const account of setup.accounts) items.append(accountPolicy(account));
    details.append(title,
      element("p", "muted", "Set the minimum balance to keep and the first date you can draw from each account."),
      items, routePolicy());
    return details;
  }

  return { open };
}
