import { api, apiMessage, button, dateLabel, element, empty, euros, icon, money, options, svg } from "./ui.js";
import { mayOpenProfile } from "./workspaces.js";

function makeField(label, input) {
  const field = element("label", "field");
  field.append(element("span", "", label), input);
  return field;
}

function input(name, value = "", type = "text") {
  const control = element("input");
  control.name = name;
  control.type = type;
  control.value = value;
  return control;
}

function panelTitle(title, subtitle) {
  const group = element("div");
  group.append(element("h2", "", title), element("p", "muted", subtitle));
  return group;
}

function dialogShell(title, formId) {
  const form = element("form");
  form.id = formId;
  const titleId = formId + "-heading";
  const heading = element("h2", "", title);
  heading.id = titleId;
  const dialog = element("dialog", "editor");
  dialog.setAttribute("aria-labelledby", titleId);
  const close = button("", "icon-link", "close", "Close");
  close.addEventListener("click", () => dialog.close());
  const top = element("div", "dialog-heading");
  top.append(heading, close);
  const problem = element("p", "notice");
  problem.hidden = true;
  problem.setAttribute("role", "alert");
  form.append(top, problem);
  dialog.append(form);
  return { dialog, form, problem };
}

function reportProblem(error, output) {
  output.textContent = apiMessage(error);
  output.hidden = false;
}

function deadline(goal) {
  if (BigInt(goal.funded) >= BigInt(goal.target)) return "Funded";
  if (!goal.estimated_on) return "No estimated completion in five years";
  return "Est. completion " + dateLabel(goal.estimated_on);
}

function beforeAfter(value, funded) {
  return value ? dateLabel(value) : money(funded) + " projected in five years";
}

function contributionAdvice(goal, accounts) {
  const assessment = goal.recommendation;
  const compact = {
    deadline_missing: "Set a target date for a monthly suggestion.",
    deadline_passed: "The target date has passed.",
    outside_horizon: "The target date is beyond this five-year forecast.",
  };
  if (compact[assessment.status]) return element("p", "muted", compact[assessment.status]);
  if (assessment.status === "funded" && assessment.configured_end === undefined) return null;
  const monthly = assessment.suggested_monthly;
  const label = {
    funding_gap: "Funding gap by target date",
    deadline_too_soon: "No monthly allocation before the target",
    competition: "Current pace competes for the same savings",
    risky: "Cash reserve at risk",
  }[assessment.status] ?? (
    monthly === "0" ? "No additional allocation needed" : "Suggested pace up to " + money(monthly) + " per month"
  );
  const body = element("details", "goals-advice");
  body.dataset.tone = ["funding_gap", "deadline_too_soon", "competition", "risky"].includes(assessment.status)
    ? "attention" : "neutral";
  body.append(element("summary", "", label));
  const details = element("div", "goals-advice-detail");
  if (assessment.additional_needed !== "0") {
    details.append(element("p", "", money(assessment.additional_needed)
      + " additional funding needed by " + dateLabel(goal.target_on) + " to protect other goals’ allocations."));
  } else if (assessment.status === "risky" && monthly !== "0") {
    details.append(element("p", "", "Modeled pace up to " + money(monthly) + " per month."));
  }
  const installmentLabel = assessment.installments === 1 ? "month-end" : "month-ends";
  details.append(element("p", "", money(assessment.remaining) + " remaining today across "
    + assessment.installments + " " + installmentLabel + ". Current allocations project "
    + money(assessment.configured_end) + " by the target date."));
  const scheduled = BigInt(assessment.scheduled_net);
  details.append(element("p", "muted", money(assessment.unassigned_today) + " unassigned now · "
    + (scheduled > 0n ? "+" : "") + money(scheduled) + " net scheduled cash · "
    + money(assessment.competing_monthly) + " per month requested by competing goals."));
  if (assessment.retained_earmarks !== goal.funded) {
    details.append(element("p", "muted", money(assessment.retained_earmarks)
      + " of the existing earmarks is projected to be backed on the target date after other allocations."));
  }
  if (assessment.first_configured !== null && BigInt(assessment.first_configured) < BigInt(assessment.configured_monthly)
      && BigInt(assessment.first_configured) < BigInt(assessment.remaining)) {
    details.append(element("p", "goals-warning", "The next month can allocate " + money(assessment.first_configured)
      + " of your " + money(assessment.configured_monthly) + " request."));
  } else if (monthly !== null && BigInt(assessment.configured_monthly) > BigInt(monthly)) {
    details.append(element("p", "muted", "Your current request is " + money(assessment.configured_monthly)
      + " per month. This forecast reaches the target with a lower monthly cap when available cash is allocated as shown."));
  }
  if (assessment.reserve !== null && assessment.configured_reserve) {
    const reserve = BigInt(assessment.reserve);
    const old = assessment.configured_reserve;
    const suggested = assessment.suggested_reserve;
    const low = (name, result) => element("p", BigInt(result.minimum) < reserve ? "goals-warning" : "muted",
      name + " " + money(result.minimum) + " of free cash on " + dateLabel(result.on)
      + (BigInt(result.minimum) < reserve ? " · below " + money(assessment.reserve) + " reserve" : ""));
    details.append(low("Current plan low", old));
    if (suggested && (suggested.minimum !== old.minimum || suggested.on !== old.on)) {
      details.append(low("Suggested pace low", suggested));
    }
  }
  if (assessment.suggested_schedule.length) {
    const schedule = element("details", "goals-advice-schedule");
    schedule.append(element("summary", "", "Suggested month-end amounts"));
    const dates = element("ul");
    for (const month of assessment.suggested_schedule) {
      const amounts = month.links.map(link => money(link.amount) + " from "
        + accounts.find(account => account.id === link.account_id).name);
      const row = element("li", "", dateLabel(month.on) + " · " + amounts.join(" · "));
      dates.append(row);
    }
    schedule.append(dates);
    details.append(schedule);
  }
  details.append(element("p", "muted", "Suggestions use savings forecast to remain available at each month-end through the target date, after competing allocations."));
  body.append(details);
  return body;
}

function renderGraph(data, editGoal) {
  const panel = element("section", "panel goals-panel");
  const heading = element("div", "panel-heading", panelTitle(
    "Savings map", "Balances come from your ledger. Lines connect each goal to its funding accounts.",
  ));
  const graph = element("div", "goals-map");
  const edges = svg("svg", { "aria-hidden": "true", class: "goals-edges" });
  const accounts = element("div", "goals-column");
  const goalColumn = element("div", "goals-column");
  const accountsHead = element("h3", "goals-column-title", "Savings accounts");
  const goalsHead = element("h3", "goals-column-title", "Goals");
  accounts.append(accountsHead);
  goalColumn.append(goalsHead);
  const accountNodes = new Map();
  const goalNodes = new Map();
  for (const account of data.accounts) {
    const node = element("article", "goals-node goals-account");
    const heading = element("h3");
    heading.append(icon("savings"), element("span", "", account.name));
    node.append(heading, element("strong", "goals-amount mono", money(account.balance)),
      element("p", "muted", money(account.unassigned) + " unassigned"));
    if (account.first_pressure_on) {
      node.append(element("p", "goals-warning", "Scheduled withdrawals may reduce coverage from "
        + dateLabel(account.first_pressure_on) + "."));
    }
    accounts.append(node);
    accountNodes.set(account.id, node);
  }
  for (const goal of data.goals) {
    const node = element("article", "goals-node");
    const heading = element("h3");
    heading.append(icon("tags"), element("span", "", goal.name));
    const titleRow = element("div", "goals-node-title");
    titleRow.append(heading);
    if (goal.profile_id && mayOpenProfile(goal.profile_id)) {
      const open = element("a", "icon-link", icon("arrow-up-right"));
      open.href = "/?workspace=" + goal.profile_id + "#goals";
      open.setAttribute("aria-label", "Open personal goal " + goal.name);
      titleRow.append(open);
    } else if (!goal.profile_id) {
      const edit = button("", "icon-link", "edit", "Edit " + goal.name);
      edit.addEventListener("click", () => editGoal(goal));
      titleRow.append(edit);
    }
    const progress = element("progress", "goals-progress");
    progress.max = 100;
    progress.value = Number(BigInt(goal.funded) * 10000n / BigInt(goal.target)) / 100;
    progress.setAttribute("aria-label", goal.name + " funding progress");
    const funds = element("p", "mono", money(goal.funded) + " of " + money(goal.target));
    const accountNames = goal.funding.map(link => data.accounts.find(account => account.id === link.account_id)?.name).join(", ");
    const status = element("p", "muted", deadline(goal));
    const monthly = goal.funding.reduce((total, link) => total + BigInt(link.monthly), 0n);
    node.append(titleRow, funds, progress, status);
    if (goal.target_on) {
      const late = goal.funded !== goal.target && (!goal.estimated_on || goal.estimated_on > goal.target_on);
      node.append(element("p", late ? "goals-warning" : "muted", "Target " + dateLabel(goal.target_on)));
    }
    node.append(element("p", "muted", money(monthly) + " per month"),
      element("p", "goals-links muted", accountNames));
    const advice = contributionAdvice(goal, data.accounts);
    if (advice) node.append(advice);
    goalColumn.append(node);
    goalNodes.set(goal.id, node);
  }
  graph.append(edges, accounts, goalColumn);
  panel.append(heading, graph);
  const observe = new ResizeObserver(() => {
    const box = graph.getBoundingClientRect();
    if (!box.width) return;
    const lines = [];
    for (const goal of data.goals) {
      const to = goalNodes.get(goal.id).getBoundingClientRect();
      for (const link of goal.funding) {
        const from = accountNodes.get(link.account_id).getBoundingClientRect();
        const x = from.right - box.left;
        const y = (from.top + from.bottom) / 2 - box.top;
        const dx = to.left - box.left;
        const dy = (to.top + to.bottom) / 2 - box.top;
        const mid = x + (dx - x) / 2;
        const strength = Number(BigInt(link.backed) * 100n / BigInt(goal.target)) / 100;
        lines.push(svg("path", {
          d: "M " + x + " " + y + " C " + mid + " " + y + " " + mid + " " + dy + " " + dx + " " + dy,
          "stroke-width": 1.5 + strength * 6,
          class: "goals-edge",
        }));
      }
    }
    edges.replaceChildren(...lines);
  });
  observe.observe(graph);
  for (const node of [...accountNodes.values(), ...goalNodes.values()]) observe.observe(node);
  return { panel, observe };
}

export function setupGoals(root, { notify = () => {} } = {}) {
  let current;
  let loading;
  let graphObserver;
  let reserve = "";

  const header = element("div", "page-heading");
  const title = element("div");
  title.append(element("h1", "", "Savings goals"),
    element("p", "page-description", "See which accounts fund your goals and where to direct your next contribution."));
  header.append(title);
  const live = element("p", "notice");
  live.hidden = true;
  live.setAttribute("role", "alert");
  const content = element("div");
  root.append(header, live, content);

  async function load(reserveInput = reserve, feedback = live) {
    loading?.abort();
    const request = new AbortController();
    loading = request;
    if (!current) content.replaceChildren(element("p", "muted", "Loading your savings goals…"));
    try {
      const query = reserveInput ? "?" + new URLSearchParams({ reserve: reserveInput }) : "";
      const data = await api("/api/goals" + query, { signal: request.signal });
      if (request.signal.aborted) return;
      reserve = reserveInput;
      current = data;
      live.hidden = true;
      render();
    } catch (error) {
      if (!request.signal.aborted) {
        reportProblem(error, feedback);
        if (!current) {
          const retry = button("Reload goals");
          retry.addEventListener("click", open);
          content.replaceChildren(retry);
        }
      }
    }
  }

  function open() {
    return load();
  }

  function reserveControls() {
    const form = element("form", "goals-reserve");
    const control = input("reserve", reserve);
    control.inputMode = "decimal";
    control.placeholder = "Optional";
    const apply = button("Check reserve", "secondary");
    apply.type = "submit";
    const feedback = element("p", "notice");
    feedback.setAttribute("role", "alert");
    feedback.hidden = true;
    form.append(makeField("Minimum free cash (€)", control), apply, feedback);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      feedback.hidden = true;
      apply.disabled = true;
      try {
        await load(control.value.trim(), feedback);
      } finally {
        apply.disabled = false;
      }
    });
    return form;
  }

  function render() {
    graphObserver?.disconnect();
    const data = current;
    const add = button("Add goal", "primary", "plus");
    add.disabled = !data.accounts.length;
    add.addEventListener("click", () => editGoal());
    if (!data.accounts.length) {
      const accountsLink = element("a", "secondary", "Open accounts");
      accountsLink.href = "#accounts";
      const card = empty("savings", "Add a savings account", "Link a savings account from your ledger to a goal.", accountsLink);
      card.classList.add("panel");
      content.replaceChildren(card);
      return;
    }
    if (!data.goals.length) {
      const card = empty("tags", "Start a savings goal", "Set a target and earmark cash from one or more savings accounts.", add);
      card.classList.add("panel");
      content.replaceChildren(card);
      return;
    }
    const controls = element("div", "goals-actions");
    controls.append(add);
    if (commonAccounts().length) {
      const redirect = button("Redirect contribution", "secondary", "transfer");
      redirect.addEventListener("click", redirectDialog);
      controls.append(redirect);
    }
    if (data.goals.some(goal => goal.target_on)) controls.append(reserveControls());
    const graph = renderGraph(data, editGoal);
    graphObserver = graph.observe;
    const description = element("p", "muted goals-note",
      "Monthly allocations start next month and use unassigned savings, including scheduled deposits and withdrawals. The estimate shows when each goal first reaches its target within five years.");
    content.replaceChildren(controls, graph.panel, description);
  }

  function editGoal(goal) {
    const { dialog, form, problem } = dialogShell(goal ? "Edit savings goal" : "Add savings goal", "goal-editor");
    const fields = element("div", "field-grid");
    const name = input("name", goal?.name);
    name.required = true;
    name.maxLength = 80;
    const target = input("target", goal ? euros(goal.target) : "");
    target.inputMode = "decimal";
    target.required = true;
    const dueDate = input("target_on", goal?.target_on ?? "", "date");
    fields.append(makeField("Goal name", name), makeField("Target amount (€)", target), makeField("Target date (optional)", dueDate));

    const funding = element("div", "goals-funding-list");
    const linkRows = [];
    const addAccount = button("Link savings account", "secondary", "plus");
    function addLink(existing) {
      const row = element("fieldset", "goals-funding");
      const account = element("select");
      account.required = true;
      const available = new Map(current.accounts.map(account => [account.id, account]));
      const reserved = new Set(linkRows.map(item => Number(item.account.value)));
      const first = current.accounts.find(item => !reserved.has(item.id));
      options(account, current.accounts.map(item =>
        [item.id, item.name + " · " + money(item.unassigned) + " unassigned"]), existing?.account_id ?? first?.id);
      const earmarked = input("earmarked", existing ? euros(existing.earmarked) : "0.00");
      const monthly = input("monthly", existing ? euros(existing.monthly) : "0.00");
      for (const control of [earmarked, monthly]) {
        control.required = true;
        control.inputMode = "decimal";
      }
      const summary = element("p", "muted");
      function refreshSummary() {
        const item = available.get(Number(account.value));
        const previous = goal?.funding.find(link => link.account_id === item?.id);
        summary.textContent = previous
          ? money(previous.backed) + " of " + money(previous.earmarked) + " backed"
          : (item ? money(item.unassigned) + " available to earmark" : "");
      }
      account.addEventListener("change", refreshSummary);
      refreshSummary();
      const remove = button("", "icon-link", "close", "Remove account link");
      const link = { account, earmarked, monthly, remove };
      remove.addEventListener("click", () => {
        linkRows.splice(linkRows.indexOf(link), 1);
        row.remove();
        updateLinks();
      });
      const title = element("div", "goals-funding-title");
      title.append(makeField("Savings account", account), remove);
      const amounts = element("div", "field-grid");
      amounts.append(makeField("Earmarked now (€)", earmarked), makeField("Monthly allocation (€)", monthly));
      row.append(title, amounts, summary);
      funding.append(row);
      linkRows.push(link);
      updateLinks();
    }
    function updateLinks() {
      addAccount.disabled = linkRows.length >= current.accounts.length;
      linkRows.forEach(row => { row.remove.disabled = linkRows.length < 2; });
    }
    addAccount.addEventListener("click", () => addLink());
    if (goal) goal.funding.forEach(addLink);
    else addLink();

    const actions = element("div", "dialog-actions");
    if (goal) {
      const remove = button("Delete goal", "danger-button", "trash");
      remove.addEventListener("click", async () => {
        if (!window.confirm("Delete this savings goal and release its earmarks?")) return;
        remove.disabled = true;
        try {
          await api("/api/goals/" + goal.id, { method: "DELETE" });
          dialog.close();
          notify("Savings goal deleted.");
          open();
        } catch (error) {
          reportProblem(error, problem);
          remove.disabled = false;
        }
      });
      actions.append(remove);
    }
    const submit = button("Save goal", "primary");
    submit.type = "submit";
    const save = element("div", "dialog-save", submit);
    actions.append(save);
    form.append(fields, headingWithText("Funding", "Earmarking sets money aside within an account. It leaves the account’s balance intact."),
      funding, addAccount, actions);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      submit.disabled = true;
      problem.hidden = true;
      try {
        await api(goal ? "/api/goals/" + goal.id : "/api/goals", { method: goal ? "PUT" : "POST", data: {
          name: name.value, target: target.value, target_on: dueDate.value,
          funding: linkRows.map(link => ({ account_id: link.account.value, earmarked: link.earmarked.value, monthly: link.monthly.value })),
        } });
        dialog.close();
        notify("Savings goal saved.");
        open();
      } catch (error) {
        reportProblem(error, problem);
        submit.disabled = false;
      }
    });
    installDialog(dialog);
  }

  function commonAccounts() {
    return current.accounts.filter(account => {
      const links = current.goals.filter(goal => !goal.profile_id)
        .flatMap(goal => goal.funding.filter(link => link.account_id === account.id));
      return links.length >= 2 && links.some(link => BigInt(link.monthly) > 0n);
    });
  }

  function redirectDialog() {
    const { dialog, form, problem } = dialogShell("Redirect monthly contribution", "goal-redirect");
    const account = element("select");
    const from = element("select");
    const to = element("select");
    const amount = input("amount");
    amount.inputMode = "decimal";
    amount.required = true;
    const percent = input("percent", "50", "range");
    percent.min = "0";
    percent.max = "100";
    percent.step = "1";
    const eligibleAccounts = commonAccounts();
    options(account, eligibleAccounts.map(item => [item.id, item.name]));
    const preview = element("div", "goals-preview");
    preview.setAttribute("aria-live", "polite");
    const applyButton = button("Apply redirect", "primary");
    applyButton.disabled = true;
    const previewButton = button("Preview impact");
    let request;
    let approved;
    const fields = element("div", "field-grid");
    fields.append(makeField("Savings account", account), makeField("From goal", from), makeField("To goal", to),
      makeField("Monthly amount to redirect (€)", amount), makeField("Percentage of source allocation", percent));
    const actions = element("div", "dialog-actions");
    actions.append(previewButton, applyButton);
    form.append(fields, preview, actions);
    form.addEventListener("submit", event => event.preventDefault());

    const linksFor = goal => goal?.funding.find(link => link.account_id === Number(account.value));
    function currentLink(id) {
      return linksFor(current.goals.find(goal => goal.id === Number(id)));
    }
    function clearPreview() {
      request?.abort();
      approved = null;
      preview.replaceChildren();
      applyButton.disabled = true;
      previewButton.disabled = false;
      problem.hidden = true;
    }
    function setAmount() {
      const link = currentLink(from.value);
      amount.value = euros(BigInt(link?.monthly ?? 0) * BigInt(percent.value) / 100n);
      clearPreview();
    }
    function updateTargets() {
      const items = current.goals.filter(goal => !goal.profile_id && goal.id !== Number(from.value) && linksFor(goal));
      options(to, items.map(goal => [goal.id, goal.name]));
      setAmount();
    }
    function updateSources() {
      const items = current.goals.filter(goal => !goal.profile_id && BigInt(linksFor(goal)?.monthly ?? 0) > 0n);
      options(from, items.map(goal => [goal.id, goal.name + " · " + money(linksFor(goal).monthly) + "/month"]));
      updateTargets();
    }
    account.addEventListener("change", updateSources);
    from.addEventListener("change", updateTargets);
    to.addEventListener("change", clearPreview);
    percent.addEventListener("input", setAmount);
    amount.addEventListener("input", clearPreview);
    updateSources();

    previewButton.addEventListener("click", async () => {
      clearPreview();
      if (!form.reportValidity()) return;
      const data = {
        account_id: account.value, from_id: from.value, to_id: to.value, amount: amount.value,
        expected_from: euros(currentLink(from.value).monthly), expected_to: euros(currentLink(to.value).monthly),
      };
      const pending = new AbortController();
      request = pending;
      previewButton.disabled = true;
      try {
        const result = await api("/api/goals/preview", { method: "POST", data, signal: pending.signal });
        if (pending.signal.aborted) return;
        const changed = result.goals.filter(goal => goal.id === Number(data.from_id) || goal.id === Number(data.to_id)
          || goal.before_on !== goal.estimated_on || goal.before_projected_funded !== goal.projected_funded);
        preview.replaceChildren(...changed.map(goal => {
          const row = element("div", "goals-preview-row");
          row.append(element("strong", "", goal.name), element("p", "", "Original "
            + beforeAfter(goal.before_on, goal.before_projected_funded) + " → New "
            + beforeAfter(goal.estimated_on, goal.projected_funded)));
          return row;
        }));
        approved = data;
        applyButton.disabled = false;
      } catch (error) {
        if (!pending.signal.aborted) reportProblem(error, problem);
      } finally {
        if (!pending.signal.aborted) previewButton.disabled = false;
      }
    });
    applyButton.addEventListener("click", async () => {
      if (!approved) return;
      applyButton.disabled = true;
      try {
        await api("/api/goals/redirect", { method: "POST", data: approved });
        dialog.close();
        notify("Monthly contribution redirected.");
        open();
      } catch (error) {
        reportProblem(error, problem);
        applyButton.disabled = false;
      }
    });
    dialog.addEventListener("close", () => request?.abort(), { once: true });
    installDialog(dialog);
  }

  function installDialog(dialog) {
    const trigger = document.activeElement;
    root.append(dialog);
    dialog.addEventListener("close", () => {
      dialog.remove();
      if (trigger?.isConnected) trigger.focus();
    }, { once: true });
    dialog.showModal();
  }

  function headingWithText(title, copy) {
    const node = element("div", "goals-field-heading");
    node.append(element("h3", "", title), element("p", "muted", copy));
    return node;
  }

  return { open };
}
