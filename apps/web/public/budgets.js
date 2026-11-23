import { api, apiMessage, button, categoryPath, dateLabel, element, euros, money, options } from "./ui.js";

const base = "/api/budgets";

function issue(error) {
  const node = element("p", "notice", apiMessage(error));
  node.setAttribute("role", "alert");
  return node;
}

function monthName(month) {
  return dateLabel(month + "-01", { month: "long", year: "numeric" });
}

function offsetMonth(month, offset) {
  const day = new Date(month + "-01T00:00:00Z");
  day.setUTCMonth(day.getUTCMonth() + offset);
  return day.toISOString().slice(0, 7);
}

function field(root, name, label, { pairs, value, ...properties } = {}) {
  const control = element(pairs ? "select" : "input");
  control.name = name;
  if (pairs) options(control, pairs, value);
  else if (value != null) control.value = value;
  Object.assign(control, properties);
  const wrapper = element("label", "field", element("span", "", label));
  wrapper.append(control);
  root.append(wrapper);
  return control;
}

function metric(label, value) {
  const node = element("div", "budget-metric");
  node.append(element("strong", "", value), element("span", "muted", label));
  return node;
}

function moneyProgress(spent, maximum) {
  const filled = BigInt(spent) * 1000n / BigInt(maximum);
  return Number(filled > 1000n ? 1000n : filled) / 10;
}

function table(columns, values) {
  const grid = element("table");
  const row = element("tr");
  const body = element("tbody");
  for (const column of columns) {
    const cell = element("th", "", column);
    cell.scope = "col";
    row.append(cell);
  }
  for (const cells of values) {
    const record = element("tr");
    record.append(...cells.map(value => element("td", "", value)));
    body.append(record);
  }
  grid.append(element("thead", "", row), body);
  return element("div", "table-scroll", grid);
}

export function setupBudgets(root, { notify = () => {} } = {}) {
  let selectedMonth;
  let request = 0;
  const layout = element("div", "budget-layout");
  const header = element("header", "page-heading");
  const introduction = element("div", "", element("h1", "", "Spending limits"));
  introduction.append(element("p", "page-description", "Set monthly family targets and review posted and scheduled spending."));
  header.append(introduction);
  const controls = element("div", "budget-actions");
  header.append(controls);
  root.replaceChildren(header, layout);

  async function open() {
    const generation = ++request;
    layout.replaceChildren(element("p", "muted", "Loading spending limits…"));
    try {
      const search = selectedMonth ? "?" + new URLSearchParams({ month: selectedMonth }) : "";
      const state = await api(base + search);
      if (generation !== request) return;
      selectedMonth = state.month;
      drawMonthControl();
      if (state.mode === "group") drawGroup(state);
      else drawProfile(state);
    } catch (error) {
      if (generation !== request) return;
      const retry = button("Retry");
      retry.addEventListener("click", open);
      layout.replaceChildren(issue(error), retry);
    }
  }

  function drawMonthControl() {
    const previous = button("Previous");
    previous.setAttribute("aria-label", "Previous limit month");
    const next = button("Next");
    next.setAttribute("aria-label", "Next limit month");
    const month = element("input");
    month.type = "month";
    month.value = selectedMonth;
    month.min = "1900-01";
    month.max = "9998-12";
    month.setAttribute("aria-label", "Limit month");
    previous.disabled = selectedMonth === month.min;
    next.disabled = selectedMonth === month.max;
    function choose(value) {
      if (!/^\d{4}-\d{2}$/.test(value) || value < month.min || value > month.max) return;
      selectedMonth = value;
      open();
    }
    previous.addEventListener("click", () => choose(offsetMonth(selectedMonth, -1)));
    next.addEventListener("click", () => choose(offsetMonth(selectedMonth, 1)));
    month.addEventListener("change", () => choose(month.value));
    controls.replaceChildren(previous, month, next);
  }

  function drawGroup(state) {
    const venue = element("section", "panel budget-panel");
    const heading = element("div", "panel-heading");
    heading.append(element("h2", "", state.group_name + " limits · " + monthName(state.month)));
    const add = button("New spending limit", "primary", "plus");
    heading.append(add);
    venue.append(heading);
    const formHost = element("div", "budget-editor");
    venue.append(formHost);
    add.addEventListener("click", () => drawEditor(formHost, state));
    const reports = element("div", "budget-grid");
    layout.replaceChildren(venue, reports);
    if (!state.limits.length) {
      reports.append(element("p", "muted", "No spending limits cover " + monthName(state.month) + "."));
    }
    for (const limit of state.limits) reports.append(limitCard(state, limit, {
      edit: () => {
        drawEditor(formHost, state, limit);
        formHost.querySelector("input")?.focus();
      },
      end: () => endLimit(state, limit),
    }));
  }

  function drawProfile(state) {
    layout.replaceChildren();
    if (!state.groups.length) {
      const empty = element("section", "panel budget-panel", element("h2", "", "Family targets"));
      empty.append(element("p", "muted", "Limits from your family workspaces will appear here."));
      layout.append(empty);
      return;
    }
    for (const group of state.groups) {
      const heading = element("h2", "budget-group-heading", group.group_name + " · " + monthName(group.month));
      const grid = element("div", "budget-grid");
      layout.append(heading, grid);
      if (!group.limits.length) grid.append(element("p", "muted", "No limits apply to this profile this month."));
      for (const limit of group.limits) grid.append(limitCard(group, limit));
    }
  }

  function drawEditor(host, state, limit = null) {
    const form = element("form", "field-grid");
    const categoryMap = new Map(state.categories.map(category => [category.id, category]));
    const title = element("h3", "", limit ? "Edit spending limit" : "New spending limit");
    field(form, "name", "Limit name", {
      required: true, maxLength: 80, placeholder: "e.g. Household groceries", value: limit?.name ?? "",
    });
    field(form, "monthly_limit", "Monthly cap in euros", {
      required: true, inputMode: "decimal", placeholder: "0.00", value: limit ? euros(limit.monthly_limit) : "",
    });
    const memberOptions = [["", "Everyone"], ...state.members.map(member => [member.id, member.name])];
    if (limit?.member_id && !state.members.some(member => member.id === limit.member_id)) {
      memberOptions.push([limit.member_id, "Former member"]);
    }
    field(form, "member_id", "Who is covered?", { pairs: memberOptions, value: limit?.member_id });
    const categoryOptions = [["", "All expenses"],
      ...state.categories.map(category => [category.id, categoryPath(category, categoryMap)])];
    if (limit?.category_id && !categoryMap.has(limit.category_id)) {
      categoryOptions.push([limit.category_id, "Unavailable category"]);
    }
    field(form, "category_id", "Expense category", { pairs: categoryOptions, value: limit?.category_id });
    field(form, "starts_month", "First month", {
      type: "month", required: true, min: "1900-01", max: "9998-12", value: limit?.starts_month ?? state.month,
    });
    field(form, "ends_month", "Last month (optional)", {
      type: "month", min: "1900-01", max: "9998-12", value: limit?.ends_month ?? "",
    });
    const feedback = element("div", "budget-editor-foot");
    const cancel = button("Cancel");
    cancel.addEventListener("click", () => host.replaceChildren());
    const save = button(limit ? "Save changes" : "Save limit", "primary");
    save.type = "submit";
    const actions = element("div", "budget-actions budget-editor-foot");
    actions.append(cancel, save);
    form.append(feedback, actions);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      save.disabled = true;
      feedback.replaceChildren();
      try {
        await api(base + (limit ? "/" + limit.id : ""), {
          method: limit ? "PUT" : "POST",
          data: Object.fromEntries(new FormData(form)),
        });
        notify("Spending limit saved.");
        await open();
      } catch (error) { feedback.replaceChildren(issue(error)); }
      finally { save.disabled = false; }
    });
    host.replaceChildren(title, form);
    form.elements.name.focus();
  }

  async function endLimit(state, limit) {
    if (!window.confirm("End " + limit.name + " after " + monthName(state.month) + "?")) return;
    try {
      await api(base + "/" + limit.id + "?" + new URLSearchParams({ last_month: state.month }), { method: "DELETE" });
      notify("Spending limit ended after " + monthName(state.month) + ".");
      await open();
    } catch (error) { layout.prepend(issue(error)); }
  }

  function limitCard(state, limit, actions = null) {
    const member = state.members.find(item => item.id === limit.member_id);
    const memberLabel = member?.name ?? (limit.member_id == null ? "Everyone" : "Former member");
    const category = limit.category_name || (limit.category_id == null ? "All expenses" : "Unavailable category");
    const card = element("section", "panel budget-panel budget-limit");
    const title = element("div", "", element("h3", "", limit.name));
    title.append(element("p", "muted", memberLabel + " · " + category));
    card.append(title);

    const status = limit.exceeded ? "Over the limit" : limit.projected_exceeded ? "Scheduled spending would exceed the limit"
      : limit.remaining === "0" ? "Limit reached" : "Within the limit";
    const tag = element("p", limit.exceeded ? "budget-status over" : limit.projected_exceeded ? "budget-status pending" : "budget-status", status);
    card.append(tag);
    const bar = element("progress", "budget-progress");
    bar.max = 100;
    bar.value = moneyProgress(limit.spent, limit.monthly_limit);
    bar.setAttribute("aria-label", limit.name + " limit used");
    bar.setAttribute("aria-valuetext", money(limit.spent) + " spent out of " + money(limit.monthly_limit));
    if (limit.exceeded) bar.classList.add("over");
    card.append(bar);
    const totals = element("div", "budget-totals");
    totals.append(metric("Posted", money(limit.spent)), metric("Monthly limit", money(limit.monthly_limit)),
      metric(BigInt(limit.remaining) < 0n ? "Over by" : "Remaining", money(BigInt(limit.remaining) < 0n ? -BigInt(limit.remaining) : limit.remaining)));
    if (BigInt(limit.scheduled) > 0n) {
      totals.append(metric("Scheduled", money(limit.scheduled)), metric("Projected total", money(limit.projected)));
    }
    if (limit.own_spent !== undefined && limit.member_id == null) totals.append(metric("Your share", money(limit.own_spent)));
    card.append(totals);
    if (limit.members.length) {
      const members = element("div", "budget-members");
      for (const member of limit.members) {
        const detail = element("span", "", member.name);
        detail.append(element("strong", "", money(member.amount)));
        members.append(detail);
      }
      card.append(members);
    }
    if (limit.entries.length) {
      const detail = element("details", "budget-entries", element("summary", "", "View posted expenses (" + limit.entries.length + ")"));
      const memberNames = new Map(state.members.map(item => [item.id, item.name]));
      detail.append(table(["Date", "Expense", "Member", "Amount"], limit.entries.map(entry => [
        dateLabel(entry.posted_on), entry.name, memberNames.get(entry.member_id) ?? "Group ledger", money(entry.amount),
      ])));
      card.append(detail);
    }
    if (actions) {
      const footer = element("div", "budget-actions");
      const edit = button("Edit limit");
      const end = button("End after this month");
      edit.addEventListener("click", actions.edit);
      end.disabled = limit.ends_month === state.month;
      end.addEventListener("click", actions.end);
      footer.append(edit, end);
      card.append(footer);
    }
    return card;
  }

  return { open };
}
