import { accountTypes, api, apiMessage, button, categoryPath, dateLabel, element, empty, icon, money, options } from "./ui.js";
import { mayOpenProfile } from "./workspaces.js";

const apiPath = "/api/returns";
const instrumentKinds = new Set(["savings", "investment"]);
const cashKinds = new Set(["checking", "cash", "savings"]);
const products = { deposit: "Deposit interest", fixed_income: "Fixed-income payout" };

function field(form, label, control, name) {
  const wrap = element("label", "field");
  control.name = name;
  wrap.append(element("span", "", label), control);
  form.append(wrap);
  return { wrap, control };
}

function input(type = "text", value = "") {
  const node = element("input");
  node.type = type;
  node.value = value;
  return node;
}

function submit(label, style = "primary") {
  const node = button(label, style);
  node.type = "submit";
  return node;
}

function alertNode() {
  const output = element("p", "notice returns-notice");
  output.setAttribute("role", "alert");
  output.hidden = true;
  return output;
}

function showError(output, error) {
  output.textContent = apiMessage(error);
  output.hidden = false;
}

function dialogShell(root, id, title) {
  const heading = element("h2", "", title);
  heading.id = id;
  const dialog = element("dialog", "editor returns-editor");
  dialog.setAttribute("aria-labelledby", id);
  const close = button("", "icon-link", "close", "Close dialog");
  close.addEventListener("click", () => dialog.close());
  const top = element("div", "dialog-heading");
  top.append(heading, close);
  const error = alertNode();
  const content = element("div");
  dialog.append(top, error, content);
  const trigger = document.activeElement;
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (trigger?.isConnected) trigger.focus();
  }, { once: true });
  root.append(dialog);
  return { dialog, content, error };
}

function isEnded(rule, today) {
  return Boolean(rule.archived_on || rule.end_on && rule.end_on < today);
}

function accountName(account) {
  return account.profile_name ? account.name + " · " + account.profile_name : account.name;
}

function accountOpenHref(accountId, profileId) {
  return (profileId ? "/?workspace=" + encodeURIComponent(profileId) : "") + "#returns/account/" + accountId;
}

function ruleDue(rule) {
  return rule.due ?? (rule.due_on ? [rule.due_on] : []);
}

export function setupReturns(root, { notify = () => {}, onChange = () => {} } = {}) {
  let state;
  let loading;
  let instrumentFilter = "";
  let activeTab = "active";

  const header = element("div", "page-heading");
  const title = element("div");
  title.append(element("h1", "", "Monthly returns"),
    element("p", "page-description", "Schedule deposit interest and fixed-income payouts, then record each payment received."));
  const add = button("Add monthly return", "primary", "plus");
  add.disabled = true;
  add.addEventListener("click", () => addSchedule());
  header.append(title, add);
  const error = alertNode();
  const body = element("div", "returns-body");
  root.append(header, error, body);

  async function load() {
    loading?.abort();
    const request = new AbortController();
    loading = request;
    error.hidden = true;
    if (!state) body.replaceChildren(element("p", "muted", "Loading monthly returns…"));
    try {
      const data = await api(apiPath, { signal: request.signal });
      if (request.signal.aborted) return;
      state = data;
      render();
    } catch (failure) {
      if (request.signal.aborted) return;
      showError(error, failure);
      if (!state) {
        const retry = button("Reload returns");
        retry.addEventListener("click", load);
        body.replaceChildren(retry);
      }
    }
  }

  function instruments() {
    return state.accounts.filter(account => instrumentKinds.has(account.kind) && !account.profile_id);
  }

  function instrumentFilterChoices() {
    const productAccounts = new Set(state.returns.map(rule => rule.instrument_id));
    return state.accounts.filter(account => instrumentKinds.has(account.kind) || productAccounts.has(account.id));
  }

  function render() {
    const eligible = instruments();
    add.disabled = !eligible.length;
    if (!state.returns.length && !eligible.length) {
      const link = element("a", "secondary", "Open accounts");
      link.href = "#accounts";
      body.replaceChildren(empty("savings", "Choose a savings or investment account",
        "Add a savings or investment account before creating a monthly return schedule.", link));
      return;
    }
    const accounts = new Map(state.accounts.map(account => [account.id, account]));
    if (instrumentFilter && !accounts.has(Number(instrumentFilter))) instrumentFilter = "";
    const scoped = state.returns.filter(rule => !instrumentFilter || String(rule.instrument_id) === instrumentFilter);
    const active = scoped.filter(rule => !isEnded(rule, state.today));
    const ended = scoped.filter(rule => isEnded(rule, state.today));
    const endedDue = ended.filter(rule => ruleDue(rule).length).length;
    const controlRow = element("div", "returns-filter");
    const filter = element("select");
    options(filter, instrumentFilterChoices().map(account => [account.id, accountName(account)]),
      instrumentFilter, "All instruments");
    filter.addEventListener("change", () => {
      instrumentFilter = filter.value;
      activeTab = "active";
      render();
    });
    field(controlRow, "Funding instrument", filter, "instrument_filter");
    const segments = element("div", "segmented returns-tabs");
    segments.setAttribute("aria-label", "Schedule status");
    segments.setAttribute("role", "group");
    for (const [key, label] of [["active", "Active · " + active.length], ["ended", "Ended · " + ended.length]]) {
      const tab = button(label);
      tab.dataset.tab = key;
      tab.setAttribute("aria-pressed", String(activeTab === key));
      tab.addEventListener("click", () => {
        activeTab = key;
        render();
        body.querySelector('[data-tab="' + key + '"]')?.focus();
      });
      segments.append(tab);
    }
    controlRow.append(segments);
    const note = element("p", "returns-note muted",
      "Scheduled returns feed the forecast. Posting or matching a receipt updates your ledger and clears the scheduled forecast entry.");
    const visible = activeTab === "active" ? active : ended;
    const list = element("div", "returns-list");
    if (!visible.length) {
      list.append(empty("calendar", activeTab === "active" ? "No active schedules" : "No ended schedules",
        activeTab === "active" ? "Add a monthly return for a savings or investment account." : "Ended schedules stay here for reference."));
    } else {
      for (const rule of visible) list.append(ruleCard(rule, accounts));
    }
    body.replaceChildren(controlRow, note);
    if (endedDue && activeTab === "active") {
      const review = button("View ended schedules with payments to review", "text-button");
      review.addEventListener("click", () => { activeTab = "ended"; render(); });
      body.append(review);
    }
    body.append(list);
  }

  function ruleCard(rule, accounts) {
    const instrument = accounts.get(rule.instrument_id);
    const payout = accounts.get(rule.payout_account_id ?? rule.instrument_id);
    const retained = rule.payout_account_id == null || rule.payout_account_id === rule.instrument_id;
    const titleRow = element("div", "returns-rule-title");
    const product = rule.product ?? (instrument?.kind === "investment" ? "fixed_income" : "deposit");
    const clipped = element("div", "returns-rule-name");
    clipped.append(element("h2", "", rule.name), element("p", "muted", products[product]));
    const label = element("span", "returns-kind", icon(product === "fixed_income" ? "trend" : "savings"));
    titleRow.append(label, clipped);
    const finance = element("div", "returns-value");
    finance.append(element("strong", "mono", money(rule.amount, { fixed: true })), element("span", "muted", " per month"));
    const basis = rule.mode === "rate"
      ? rule.rate + "% monthly on " + money(rule.principal)
      : "Fixed monthly payment";
    finance.append(element("small", "muted", basis));
    const dates = element("div", "returns-next");
    dates.append(element("span", "muted", rule.next_on ? "Next forecast payment" : "Schedule"));
    dates.append(element("strong", "", rule.next_on ? dateLabel(rule.next_on)
      : isEnded(rule, state.today) ? "Ended" : "No upcoming payment"));
    const row = element("article", "panel returns-rule");
    const top = element("div", "returns-rule-main");
    top.append(titleRow, finance, dates);
    const meta = element("div", "returns-route");
    const path = element("p");
    path.append(element("strong", "", instrument?.name ?? rule.instrument_name ?? "Funding instrument"),
      document.createTextNode(retained ? " · Return retained here" : " → Paid into "),
      ...(retained ? [] : [element("strong", "", payout?.name ?? rule.payout_account_name ?? "Payout account")]));
    if (instrument?.kind === "investment" && retained) {
      path.append(document.createTextNode(" · Increases investment balance"));
    }
    const span = element("p", "muted", "First payment " + dateLabel(rule.start_on)
      + (rule.end_on ? " · Scheduled through " + dateLabel(rule.end_on) : "")
      + (rule.archived_on ? " · Ended " + dateLabel(rule.archived_on) : ""));
    if (rule.profile_id && rule.profile_name) span.append(document.createTextNode(" · " + rule.profile_name));
    meta.append(path, span);
    row.append(top, meta);

    const actionRow = element("div", "returns-rule-actions");
    const due = ruleDue(rule);
    if (rule.profile_id) {
      if (due.length) actionRow.append(element("span", "returns-due", "Payment to review"));
      if (mayOpenProfile(rule.profile_id)) {
        const owner = element("a", "secondary", "Open personal schedule");
        owner.href = rule.instrument_source_id ? accountOpenHref(rule.instrument_source_id, rule.profile_id)
          : "/?workspace=" + encodeURIComponent(rule.profile_id) + "#returns";
        actionRow.append(owner);
      } else actionRow.append(element("p", "muted", "Shared in this group"));
    } else {
      if (due.length) {
        const status = due.length === 1 ? "Payment to review" : "Payments to review";
        const dueTitle = element("strong", "returns-due", status);
        actionRow.append(dueTitle);
        const payments = element("div", "returns-due-dates");
        for (const scheduled of due) {
          const review = button(dateLabel(scheduled), "secondary", "calendar", "Review return due " + dateLabel(scheduled));
          review.addEventListener("click", () => recordReceipt(rule, scheduled));
          payments.append(review);
        }
        actionRow.append(payments);
      }
      if (!isEnded(rule, state.today)) {
        const end = button("End schedule", "text-button");
        end.addEventListener("click", () => endSchedule(rule));
        actionRow.append(end);
      }
    }
    if (actionRow.childElementCount) row.append(actionRow);
    if (rule.recent?.length) row.append(receiptHistory(rule));
    else if (rule.skipped_count) row.append(element("p", "muted returns-records", rule.skipped_count + " skipped"));
    return row;
  }

  function receiptHistory(rule) {
    const details = element("details", "returns-records");
    const summary = element("summary", "", rule.posted_count + " posted"
      + (rule.skipped_count ? " · " + rule.skipped_count + " skipped" : ""));
    details.append(summary);
    const history = element("ul");
    for (const item of rule.recent) {
      const label = dateLabel(item.posted_on) + " · " + money(item.amount, { fixed: true })
        + (item.posted_on !== item.due_on ? " · Due " + dateLabel(item.due_on) : "");
      history.append(element("li", "", label));
    }
    if (rule.posted_count > rule.recent.length) {
      details.append(element("p", "muted", "Most recent " + rule.recent.length + " receipts"));
    }
    details.append(history);
    return details;
  }

  function addSchedule() {
    const eligible = instruments();
    if (!eligible.length) return;
    const { dialog, content, error: failure } = dialogShell(root, "returns-editor-title", "Add monthly return");
    const form = element("form", "returns-form");
    const fields = element("div", "field-grid");
    const name = field(fields, "Schedule name", input(), "name");
    name.wrap.classList.add("full");
    name.control.required = true;
    name.control.maxLength = 100;
    name.control.placeholder = "e.g. Term deposit interest";
    const product = field(fields, "Product", element("select"), "product").control;
    options(product, Object.entries(products));
    const source = field(fields, "Instrument account", element("select"), "instrument_id").control;
    options(source, eligible.map(account => [account.id, account.name + " · " + accountTypes[account.kind].label]),
      instrumentFilter || eligible[0].id);
    const payout = field(fields, "Pay into", element("select"), "payout_account_id").control;
    const mode = field(fields, "Calculation", element("select"), "mode").control;
    options(mode, [["fixed", "Fixed amount"], ["rate", "Monthly rate on principal"]]);
    const amount = field(fields, "Fixed monthly amount (€)", input(), "amount");
    const rate = field(fields, "Monthly rate (%)", input(), "rate");
    const principal = field(fields, "Principal (€)", input(), "principal");
    for (const control of [amount.control, rate.control, principal.control]) control.inputMode = "decimal";
    const first = field(fields, "First payment date", input("date", state.today), "start_on").control;
    first.required = true;
    const last = field(fields, "Schedule end date (optional)", input("date"), "end_on").control;
    const category = field(fields, "Income category (optional)", element("select"), "category_id").control;
    const categories = state.categories.filter(item => item.kind === "income" && !item.profile_id);
    const categoryMap = new Map(categories.map(item => [item.id, item]));
    options(category, categories.map(item => [item.id, categoryPath(item, categoryMap)]), "", "No category");
    const help = element("p", "muted returns-field-note");
    const actions = element("div", "dialog-actions");
    const save = submit("Save return schedule");
    const cancel = button("Cancel");
    cancel.addEventListener("click", () => dialog.close());
    const group = element("div", "dialog-save");
    group.append(cancel, save);
    actions.append(group);
    form.append(fields, help, actions);
    content.append(form);

    let productChanged = false;
    const accounts = new Map(state.accounts.map(account => [String(account.id), account]));
    function dates() {
      const openedOn = [accounts.get(source.value)?.opened_on, accounts.get(payout.value)?.opened_on].filter(Boolean).sort();
      first.min = openedOn.at(-1) ?? "";
      last.min = first.value || first.min;
    }
    function sourceChanged() {
      if (!productChanged) product.value = accounts.get(source.value)?.kind === "investment" ? "fixed_income" : "deposit";
      options(payout, state.accounts.filter(item => !item.profile_id && String(item.id) !== source.value && cashKinds.has(item.kind))
        .map(item => [item.id, item.name]), undefined, "Keep in this instrument");
      dates();
    }
    function modeChanged() {
      const isRate = mode.value === "rate";
      for (const [row, show] of [[amount, !isRate], [rate, isRate], [principal, isRate]]) {
        row.wrap.hidden = !show;
        row.control.disabled = !show;
        row.control.required = show;
      }
      help.textContent = isRate
        ? "Enter the rate for one month. Each payment uses the original principal throughout the schedule."
        : "The schedule uses the same amount each month. Shorter months use their last day if needed.";
    }
    product.addEventListener("change", () => { productChanged = true; });
    mode.addEventListener("change", modeChanged);
    source.addEventListener("change", sourceChanged);
    payout.addEventListener("change", dates);
    first.addEventListener("change", dates);
    modeChanged();
    sourceChanged();

    form.addEventListener("submit", async event => {
      event.preventDefault();
      failure.hidden = true;
      save.disabled = true;
      try {
        const data = Object.fromEntries(new FormData(form));
        await api(apiPath, { method: "POST", data });
        activeTab = data.end_on && data.end_on < state.today ? "ended" : "active";
        if (instrumentFilter) instrumentFilter = data.instrument_id;
        dialog.close();
        await changed("Return schedule saved.");
      } catch (issue) {
        showError(failure, issue);
        save.disabled = false;
      }
    });
    dialog.showModal();
    name.control.focus();
  }

  function recordReceipt(rule, due) {
    const { dialog, content, error: failure } = dialogShell(root, "returns-receipt-title", "Review scheduled return");
    const form = element("form", "returns-form");
    const headline = element("p", "returns-receipt-description",
      rule.name + " · " + money(rule.amount, { fixed: true }) + " scheduled for " + dateLabel(due));
    const hint = element("p", "muted", "Post the scheduled amount after receipt, or match a return you already entered.");
    const match = field(form, "Ledger transaction", element("select"), "entry_id").control;
    options(match, [], "", "Post a new receipt");
    const confirm = submit("Post receipt");
    confirm.disabled = true;
    match.disabled = true;
    match.addEventListener("change", () => { confirm.textContent = match.value ? "Match receipt" : "Post receipt"; });
    const skip = button("Skip this payment", "text-button");
    const runway = element("a", "text-link", "View in cash runway");
    runway.href = "#forecast/runway/" + due;
    runway.addEventListener("click", () => dialog.close());
    const actions = element("div", "dialog-actions");
    const right = element("div", "dialog-save");
    right.append(confirm);
    actions.append(skip, right);
    form.append(actions);
    const matches = element("p", "muted", "Checking for matching ledger entries…");
    content.append(headline, hint, form, matches, runway);
    const controller = new AbortController();
    dialog.addEventListener("close", () => controller.abort(), { once: true });
    dialog.showModal();

    async function save(data, message) {
      failure.hidden = true;
      confirm.disabled = true;
      skip.disabled = true;
      try {
        await api("/api/forecast/occurrences", { method: "POST", data: { rule_id: rule.commitment_id, date: due, ...data } });
        dialog.close();
        await changed(message);
      } catch (issue) {
        showError(failure, issue);
        confirm.disabled = false;
        skip.disabled = false;
      }
    }
    form.addEventListener("submit", event => {
      event.preventDefault();
      save(match.value ? { entry_id: match.value } : {}, match.value ? "Return matched to ledger." : "Return posted to ledger.");
    });
    skip.addEventListener("click", () => save({ skip: true }, "Scheduled payment skipped."));
    const query = new URLSearchParams({ rule_id: rule.commitment_id, date: due });
    api("/api/forecast/matches?" + query, { signal: controller.signal }).then(entries => {
      if (controller.signal.aborted) return;
      options(match, entries.map(entry => [entry.id,
        entry.name + " · " + dateLabel(entry.posted_on) + " · " + money(entry.amount)]), "", "Post a new receipt");
      match.disabled = !entries.length;
      matches.textContent = entries.length ? "" : "No unmatched entries with the same payment type and account within 14 days.";
    }).catch(issue => {
      if (!controller.signal.aborted) matches.textContent = apiMessage(issue);
    }).finally(() => {
      if (!controller.signal.aborted && !skip.disabled) confirm.disabled = false;
    });
  }

  function endSchedule(rule) {
    const { dialog, content, error: failure } = dialogShell(root, "returns-end-title", "End return schedule");
    const form = element("form", "returns-form");
    form.append(element("p", "", "End future payments for " + rule.name + " from today?"));
    const actions = element("div", "dialog-actions");
    const group = element("div", "dialog-save");
    const cancel = button("Cancel");
    cancel.addEventListener("click", () => dialog.close());
    const end = submit("End schedule", "danger-button");
    group.append(cancel, end);
    actions.append(group);
    form.append(actions);
    content.append(form);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      end.disabled = true;
      failure.hidden = true;
      try {
        await api(apiPath + "/" + rule.id, { method: "DELETE" });
        activeTab = "ended";
        dialog.close();
        await changed("Return schedule ended.");
      } catch (issue) {
        showError(failure, issue);
        end.disabled = false;
      }
    });
    dialog.showModal();
    cancel.focus();
  }

  async function changed(message) {
    notify(message);
    onChange();
    await load();
  }

  function open(accountId) {
    const next = accountId == null ? "" : String(accountId);
    if (next !== instrumentFilter) activeTab = "active";
    instrumentFilter = next;
    return load();
  }

  return { open };
}
