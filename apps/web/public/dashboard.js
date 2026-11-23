import { setupAccess } from "./access.js";
import { transactionFilesDialog } from "./attachments.js";
import { authRequest, signInHref } from "./auth.js";
import { setupAuthShell } from "./auth-shell.js";
import { trendChart } from "./charts.js";
import { setupBudgets } from "./budgets.js";
import { setupCars } from "./cars.js";
import { setupDecision } from "./decision.js";
import { setupDocumentation } from "./documentation.js";
import { ledgerEditor } from "./editor.js";
import { setupFeatureHelp } from "./feature-help.js";
import { moneyFlow } from "./flow.js";
import { setupForecast } from "./forecast.js";
import { setupFunding } from "./funding.js";
import { setupGoals } from "./goals.js";
import { setupImports } from "./imports.js";
import { setupInsights, setupReplay } from "./insights.js";
import { setupPlanner } from "./planning.js";
import { setupReturns } from "./returns.js";
import { setupShared } from "./shared.js";
import { mayOpenProfile, setupWorkspaces, workspaceHref, workspaceId } from "./workspaces.js";
import { $, $$, accountTypes, api, apiMessage, button, categoryPath, dateLabel, dateRange, element, empty,
  entryTypes, flows, icon, money, options } from "./ui.js";

const titles = {
  overview: "Overview", flow: "Money flow", transactions: "Transactions",
  accounts: "Accounts", categories: "Categories", planner: "Purchase planner",
  replay: "Money flow replay", insights: "Insights", goals: "Goals", forecast: "Forecast", shared: "Shared payments",
  decision: "Decision lab", imports: "Import payments", workspaces: "Workspaces", access: "Family access",
  budgets: "Group limits", cars: "Cars", returns: "Monthly returns", funding: "Funding check", documentation: "Documentation",
};
const sections = {
  overview: ["overview", "flow", "replay", "insights"], activity: ["transactions", "imports", "shared"],
  planning: ["forecast", "funding", "planner", "goals", "returns", "decision", "budgets"],
  cars: ["cars"],
  manage: ["workspaces", "accounts", "categories", "access"],
  help: ["documentation"],
};
const modeKey = () => "quant-dashboard-mode" + (workspaceId() === "1" ? "" : "-" + workspaceId());
const filters = $("#transaction-filters");
const size = 25;
let snapshot;
let realLedger;
let activeView;
let activeHash;
let period = "6M";
let sampleMode;
let authState;
let fundingPrefill = {};
let demoRequest;
let ready = false;
let refreshRequest;
let ledgerRequest;
let ledgerPage = 1;
let ledgerPeriod = "";
let ledgerMonth = "";
let ledgerDescendants = false;
let ledgerTimer;
let toastTimer;
const chart = trendChart();
const map = moneyFlow({
  addEntry: openTransaction, editEntry: editTransaction,
  addCategory: openCategory, viewEntries: showTransactions,
});
const editor = ledgerEditor({ ledger: () => realLedger, notify, refresh });
const openTransactionFiles = transactionFilesDialog({ notify, onChange: refresh });
const planner = setupPlanner({ notify, checkFunding: openFunding });
const forecast = setupForecast($("#view-forecast"), { notify, onChange: refreshAfterLedgerChange, checkFunding: openFunding });
const funding = setupFunding($("#view-funding"), { notify, onChange: refreshAfterLedgerChange });
const documentation = setupDocumentation($("#view-documentation"), { notify, openDemo });
const featureHelp = setupFeatureHelp({ root: $("#main") });
const decision = setupDecision($("#view-decision"), { notify });
const imports = setupImports($("#view-imports"), { notify, onChange: refreshAfterLedgerChange });
const goals = setupGoals($("#view-goals"), { notify });
const returns = setupReturns($("#view-returns"), { notify, onChange: refreshAfterLedgerChange });
const budgets = setupBudgets($("#view-budgets"), { notify });
const cars = setupCars($("#view-cars"), { notify });
const workspaces = setupWorkspaces($("#view-workspaces"), { openDemo });
const access = setupAccess($("#view-access"), { notify });
const shared = setupShared($("#view-shared"), { onChange: refreshAfterLedgerChange });
const insights = setupInsights($("#view-insights"));
const replay = setupReplay($("#view-replay"), { viewEntries: showTransactions });

function readMode() {
  try {
    const mode = localStorage.getItem(modeKey());
    return mode === null ? null : mode === "sample";
  } catch {
    return null;
  }
}

function setMode(sample) {
  sampleMode = sample;
  try {
    localStorage.setItem(modeKey(), sample ? "sample" : "mine");
  } catch {
    // Mode also works without browser storage.
  }
}

function notify(message) {
  const toast = $("#toast");
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 4200);
}

function loadLedger(sample, signal) {
  return api("/api/dashboard?" + new URLSearchParams({ period, sample: sample ? "1" : "0" }), { signal });
}

async function refresh() {
  refreshRequest?.abort();
  const request = new AbortController();
  refreshRequest = request;
  const notice = $("#connection-error");
  try {
    let real;
    if (sampleMode !== true) real = await loadLedger(false, request.signal);
    const sample = sampleMode === true;
    const data = sample ? await loadLedger(true, request.signal) : real;
    if (request.signal.aborted) return false;
    if (real) realLedger = real;
    if (sampleMode === null) sampleMode = sample;
    snapshot = data;
    notice.hidden = true;
    renderSnapshot();
    return true;
  } catch (error) {
    if (!request.signal.aborted) {
      $("#connection-message").textContent = apiMessage(error);
      notice.hidden = false;
    }
    return false;
  }
}

async function switchMode(sample) {
  setMode(sample);
  resetLedgerFilters();
  return refresh();
}

function refreshAfterLedgerChange() {
  return sampleMode === true ? switchMode(false) : refresh();
}

async function useRealData() {
  if (sampleMode || snapshot?.sample) return switchMode(false);
  return realLedger ? true : refresh();
}

function updateHeader() {
  const context = $("#data-context");
  const label = $("#data-mode-label");
  label.dataset.sample = snapshot.sample;
  const demo = Boolean(document.documentElement.dataset.demoRole);
  $("span", label).textContent = snapshot.sample ? "Sample preview" : demo ? "Sample workspace" : "My data";
  $("#toggle-sample").hidden = demo;
  $("#toggle-sample").textContent = snapshot.sample ? "Use my data" : "Preview sample";
  context.hidden = usesRealWorkspace(activeView);
}

function usesRealWorkspace(view) {
  return ["goals", "forecast", "funding", "shared", "planner", "decision", "imports", "budgets", "workspaces",
    "cars", "returns", "access", "documentation"].includes(view);
}

let attentionRequest;
async function updateAttention() {
  attentionRequest?.abort();
  const panel = $("#attention-panel");
  panel.hidden = true;
  if (activeView !== "overview" || !snapshot || snapshot.sample) return;
  const request = new AbortController();
  attentionRequest = request;
  try {
    const data = await api("/api/attention", { signal: request.signal });
    if (request.signal.aborted || activeView !== "overview") return;
    const items = [];
    const link = (heading, detail, target) => {
      const card = element("a", "attention-item");
      card.href = target.startsWith("http") ? target : "#" + target;
      card.append(element("strong", "", heading), element("small", "muted", detail), icon("chevron-right"));
      items.push(card);
    };
    if (data.bill_count) {
      const first = data.bills[0];
      link(data.bill_count + (data.bill_count === 1 ? " bill in the next 7 days" : " bills in the next 7 days"),
        first.name + " · " + dateLabel(first.date) + " · " + money(-BigInt(first.amount)), "forecast/runway/" + first.date);
    }
    if (data.overdue) link(data.overdue + (data.overdue === 1 ? " overdue reimbursement" : " overdue reimbursements"),
      "Review scheduled repayments", data.overdue_profile && mayOpenProfile(data.overdue_profile)
        ? workspaceHref(data.overdue_profile, "shared") : "shared");
    if (data.next_return) link("Next investment payout", data.next_return.name + " · " + money(data.next_return.amount)
      + " · " + dateLabel(data.next_return.date), "forecast/runway/" + data.next_return.date);
    const pending = imports.pendingReview();
    if (pending) link(pending + " import rows to review", "Open the import inbox", "imports");
    if (data.low) link("Next 30 days’ projected cash low", money(data.low.amount) + " · " + dateLabel(data.low.date),
      "forecast/runway/" + data.low.date);
    for (const limit of data.limits) {
      link(limit.name, limit.group + " · " + money(limit.gap) +
        (limit.exceeded ? " over limit" : " projected over limit"), "budgets");
    }
    if (items.length) {
      $("#attention-list").replaceChildren(...items);
      panel.hidden = false;
    }
  } catch (error) {
    if (!request.signal.aborted) console.warn("Attention summary is unavailable", error.message);
  }
}

function updateMetrics() {
  for (const card of $$("[data-metric]")) {
    const metric = card.dataset.metric;
    const value = snapshot.summary[metric];
    const previous = snapshot.previous?.[metric];
    $(".metric-value", card).textContent = money(value, { fixed: true });
    const caption = $(".metric-caption", card);
    caption.textContent = {
      wealth: value === null ? "Add an account to start" : "Current balance",
      income: "Received in period", expense: "Personal cost in period", investment: "Contributions in period",
    }[metric];
    if (value === null || !previous || BigInt(previous) === 0n) continue;
    const prior = BigInt(previous);
    const change = BigInt(value) - prior;
    const scale = prior < 0n ? -prior : prior;
    const percent = (change < 0n ? -change : change) * 1000n / scale;
    const prefix = change < 0n ? "−" : change > 0n ? "+" : "";
    const label = prefix + (percent > 99990n ? "9,999+" : (Number(percent) / 10).toLocaleString()) + "%";
    const neutral = change === 0n || metric === "investment";
    const negative = (metric === "expense" ? change > 0n : change < 0n);
    const pill = element("span", "metric-change" + (neutral ? " neutral" : negative ? " down" : ""), label);
    pill.title = "Previous period: " + money(previous, { fixed: true });
    caption.replaceChildren(pill, document.createTextNode("vs. prior period"));
  }
}

function updateAllocation() {
  const node = $("#allocation");
  if (!snapshot.accounts.length) {
    const add = button("Add account", "secondary", "plus");
    add.addEventListener("click", () => openAccount());
    node.replaceChildren(empty("wallet", "No accounts yet", "Add your first cash, savings or investment balance.", add));
    addSharedBalance(node);
    return;
  }
  const groups = [
    { key: "cash", name: "Cash", color: "#37a2d6", value: 0n },
    { key: "savings", name: "Savings", color: "#22a486", value: 0n },
    { key: "investments", name: "Investments", color: "#9262cf", value: 0n },
  ];
  const total = snapshot.accounts.reduce((sum, account) => {
    const balance = BigInt(account.balance);
    if (balance <= 0n) return sum;
    const key = { savings: "savings", investment: "investments" }[account.kind] ?? "cash";
    groups.find(group => group.key === key).value += balance;
    return sum + balance;
  }, 0n);
  const donut = element("div", "allocation-donut");
  donut.title = "Share of positive balances";
  if (total > 0n) {
    const stops = [];
    let previous = 0;
    for (const [index, group] of groups.filter(item => item.value > 0n).entries()) {
      const end = previous + Number(group.value) / Number(total) * 360;
      const gap = Math.min(end - previous, 2);
      stops.push((index ? "white " + previous + "deg " + (previous + gap) + "deg, " : "")
        + group.color + " " + (index ? previous + gap : previous) + "deg " + Math.min(360, end) + "deg");
      previous = end;
    }
    donut.style.setProperty("--allocation-stops", stops.join(", "));
  }
  const count = snapshot.accounts.length;
  const center = element("div", "donut-center");
  center.append(element("strong", "", String(count)), count === 1 ? "account" : "accounts");
  donut.append(center);
  const list = element("div", "allocation-list");
  for (const group of groups) {
    const swatch = element("span", "swatch");
    swatch.style.setProperty("--swatch", group.color);
    const row = element("div", "allocation-row");
    const label = element("span", "allocation-label", swatch);
    label.append(group.name);
    row.append(label, element("span", "allocation-amount", money(snapshot.summary[group.key], { fixed: true })));
    list.append(row);
  }
  node.replaceChildren(donut, list);
  addSharedBalance(node);
}

function addSharedBalance(container) {
  if (snapshot.shared_balance === null || BigInt(snapshot.shared_balance) === 0n) return;
  const link = element("a", "text-link shared-net", "Net shared balance " + money(snapshot.shared_balance, { fixed: true }));
  link.href = "#shared";
  link.title = "Included in net worth";
  container.append(link);
}

function transactionTable(entries, detailed) {
  const ledger = snapshot;
  const labels = ["Description", "Date", "Category", "Account", ...(detailed ? ["Labels"] : []), "Amount"];
  const accounts = new Map(ledger.accounts.map(item => [item.id, item.name]));
  const categories = new Map(ledger.categories.map(item => [item.id, item]));
  const table = element("table");
  const header = element("tr");
  header.append(...labels.map(label => {
    const cell = element("th", label === "Amount" ? "ledger-amount" : "", label);
    cell.scope = "col";
    return cell;
  }));
  if (!ledger.sample) header.append(element("th", "", element("span", "sr-only", "Actions")));
  const body = element("tbody");
  for (const entry of entries) {
    const type = entryTypes[entry.kind] ?? { ...entryTypes.transfer, label: "Shared settlement" };
    const row = element("tr");
    const name = element("th");
    name.scope = "row";
    const title = element("span", "tx-title", entry.name);
    title.title = entry.note ? entry.name + " · " + entry.note : entry.name;
    const copy = element("span", "tx-name");
    copy.append(element("span", "row-icon " + type.color, icon(type.icon)), title);
    name.append(copy);
    const category = categories.get(entry.category_id);
    const categoryCell = element("td");
    if (category) {
      const pill = element("span", "pill " + category.color);
      pill.title = categoryPath(category, categories);
      pill.append(element("span", "swatch"), category.name);
      categoryCell.append(pill);
    } else {
      categoryCell.textContent = flows[entry.kind] ? "Uncategorized" : type.label;
    }
    const account = accounts.get(entry.account_id) ?? (entry.shared ? "Paid by someone else" : "—");
    const destination = accounts.get(entry.destination_id);
    row.append(name, element("td", "", dateLabel(entry.posted_on)), categoryCell,
      element("td", "", destination ? account + " → " + destination : account));
    if (detailed) {
      const chips = element("span", "tx-labels");
      chips.title = entry.labels.join(", ");
      chips.append(...entry.labels.map(label => element("span", "label-chip", label)));
      row.append(element("td", "", entry.labels.length ? chips : "—"));
    }
    const amount = entry.kind === "settlement" ? BigInt(entry.cash_delta)
      : entry.kind === "expense" ? -BigInt(entry.amount) : BigInt(entry.amount);
    const prefix = entry.kind === "income" || (entry.kind === "valuation" && amount > 0n) ? "+" : "";
    const amountCell = element("td", "ledger-amount " + entry.kind, prefix + money(amount, { fixed: true }));
    if (entry.kind === "expense" && entry.shared) {
      amountCell.append(element("small", "shared-amount-note",
        "Your share " + money(entry.amount) + " · Cash paid " + money(-BigInt(entry.cash_delta))
        + (BigInt(entry.shared_delta) >= 0n ? " · Owed back " : " · You owe ") + money(
          BigInt(entry.shared_delta) < 0n ? -BigInt(entry.shared_delta) : entry.shared_delta)));
    }
    row.append(amountCell);
    if (!ledger.sample && mayOpenProfile(entry.profile_id)) {
      const action = button("", "icon-link", entry.shared || entry.profile_id ? "arrow-up-right" : "edit",
        (entry.shared || entry.profile_id ? "Open workspace for " : "Edit ") + entry.name);
      action.addEventListener("click", () => editTransaction(entry));
      const actions = element("div", "transaction-row-actions");
      if (!entry.shared && !entry.profile_id && entry.id > 0) {
        const count = entry.attachment_count ?? 0;
        const label = "Files for " + entry.name + (count ? " (" + count + ")" : "");
        const caption = detailed ? (count ? "Files (" + count + ")" : "Attach files") : "";
        const style = detailed ? "text-button attachment-table-link" : "icon-link";
        const files = button(caption, style + (count ? " has-attachments" : ""), "paperclip", label);
        files.addEventListener("click", () => openTransactionFiles(entry));
        actions.append(files);
      }
      actions.append(action);
      row.append(element("td", "", actions));
    } else if (!ledger.sample) {
      row.append(element("td"));
    }
    body.append(row);
  }
  table.append(element("thead", "", header), body);
  const scroll = element("div", "table-scroll", table);
  scroll.tabIndex = 0;
  scroll.setAttribute("role", "region");
  scroll.setAttribute("aria-label", detailed ? "Transaction ledger table" : "Recent transactions table");
  return scroll;
}

function updateRecent() {
  const add = button("Add transaction", "secondary", "plus");
  add.addEventListener("click", () => openTransaction());
  $("#recent-transactions").replaceChildren(snapshot.recent.length ? transactionTable(snapshot.recent, false)
    : empty("transactions", "No transactions recorded", "Add an income, expense, transfer or investment.", add));
}

function updateAccounts() {
  const list = $("#account-list");
  $("#add-cash-wallet").hidden = !snapshot.sample && snapshot.accounts.some(account =>
    account.kind === "cash" && mayOpenProfile(account.profile_id));
  if (!snapshot.accounts.length) {
    const add = button("Add account", "primary", "plus");
    add.addEventListener("click", () => openAccount());
    const message = empty("wallet", "No accounts yet", "Set an opening balance and track activity against it.", add);
    message.classList.add("panel");
    list.replaceChildren(message);
    return;
  }
  list.replaceChildren(...snapshot.accounts.map(account => {
    const type = accountTypes[account.kind];
    const card = element("article", "panel account-panel");
    const heading = element("div", "panel-heading");
    const name = element("div", "account-name");
    name.append(element("span", "row-icon " + type.color, icon(type.icon)), element("h2", "", account.name));
    heading.append(name);
    if (account.profile_id && mayOpenProfile(account.profile_id)) {
      const owner = element("a", "icon-link", icon("arrow-up-right"));
      owner.href = workspaceHref(account.profile_id, "accounts");
      owner.setAttribute("aria-label", "Open " + account.profile_name + " accounts");
      heading.append(owner);
    } else if (!snapshot.sample && !account.profile_id) {
      const edit = button("", "icon-link", "edit", "Edit " + account.name);
      edit.addEventListener("click", () => editor.account(account));
      heading.append(edit);
    }
    const footer = element("footer");
    const actions = element("div", "account-card-actions");
    const view = button("View transactions", "text-button");
    view.addEventListener("click", () => showTransactions({ account: account.id }));
    actions.append(view);
    if (!snapshot.sample && account.kind === "cash" && mayOpenProfile(account.profile_id)) {
      const spend = button("Record cash expense", "text-button");
      spend.addEventListener("click", () => editor.transaction(null, { kind: "expense", account_id: account.id }));
      actions.append(spend);
      const bank = snapshot.accounts.find(item => item.profile_id === account.profile_id &&
        (item.kind === "checking" || item.kind === "savings"));
      if (bank) {
        const withdraw = button("Record cash withdrawal", "text-button");
        withdraw.addEventListener("click", () => editor.transaction(null, {
          kind: "transfer", name: "Cash withdrawal", account_id: bank.id, destination_id: account.id,
        }));
        actions.append(withdraw);
      }
    }
    const eligible = !snapshot.sample && ["savings", "investment"].includes(account.kind);
    if (eligible) {
      const terms = element("a", "text-link account-return-link", "Monthly returns");
      terms.href = "#returns/account/" + account.id;
      actions.append(terms);
    }
    footer.append(element("span", "", "Opened " + dateLabel(account.opened_on)), actions);
    card.append(heading, element("p", "metric-value", money(account.balance, { fixed: true })),
      element("p", "muted", type.label), footer);
    if (eligible) {
      const preview = element("p", "muted account-return-preview");
      preview.dataset.returnAccount = account.id;
      preview.hidden = true;
      card.append(preview);
    }
    return card;
  }));
  if (activeView === "accounts") loadAccountReturns();
}

let accountReturnRequest;
async function loadAccountReturns() {
  accountReturnRequest?.abort();
  const summaries = $$("[data-return-account]");
  if (!summaries.length || activeView !== "accounts") return;
  const request = new AbortController();
  accountReturnRequest = request;
  try {
    const data = await api("/api/returns", { signal: request.signal });
    if (request.signal.aborted || activeView !== "accounts") return;
    const byAccount = new Map();
    for (const schedule of data.returns) {
      const on = schedule.due_on || schedule.next_on;
      if (schedule.archived_on || !on) continue;
      const previous = byAccount.get(schedule.instrument_id);
      if (!previous || previous.on > on) byAccount.set(schedule.instrument_id, { on, amount: schedule.amount });
    }
    for (const summary of summaries) {
      const upcoming = byAccount.get(Number(summary.dataset.returnAccount));
      summary.hidden = !upcoming;
      if (upcoming) summary.textContent = (upcoming.on <= data.today ? "Return due " : "Next scheduled return ")
        + money(upcoming.amount) + " · " + dateLabel(upcoming.on);
    }
  } catch (error) {
    if (!request.signal.aborted) console.warn("Account return preview is unavailable", error.message);
  }
}

function updateCategories() {
  const node = $("#category-list");
  const groups = new Map();
  const categoryMap = new Map(snapshot.categories.map(item => [item.id, item]));
  for (const category of snapshot.categories) {
    const list = groups.get(category.parent_id) ?? [];
    list.push(category);
    groups.set(category.parent_id, list);
  }
  function rows(parent, depth, kind) {
    return (groups.get(parent) ?? []).filter(category => category.kind === kind).flatMap(category => {
      const row = element("div", "category-row " + category.color);
      row.style.setProperty("--depth", Math.min(depth, 7));
      const name = element("span", "", category.name);
      name.title = categoryPath(category, categoryMap);
      row.append(element("span", "swatch"), name);
      if (!snapshot.sample && !category.profile_id) {
        const add = button("", "icon-link", "plus", "Add subcategory to " + category.name);
        const edit = button("", "icon-link", "edit", "Edit " + category.name);
        add.addEventListener("click", () => openCategory(kind, category.id));
        edit.addEventListener("click", () => editor.category(category));
        row.append(add, edit);
      }
      return [row, ...rows(category.id, depth + 1, kind)];
    });
  }
  const names = { income: "Income", expense: "Expenses", investment: "Investments" };
  node.replaceChildren(...Object.entries(flows).map(([kind, type]) => {
    const card = element("article", "panel category-panel");
    const header = element("div", "panel-heading");
    const name = element("div", "account-name");
    name.append(element("span", "row-icon " + type.color, icon(type.icon)), element("h2", "", names[kind]));
    header.append(name);
    if (!snapshot.sample) {
      const add = button("", "icon-link", "plus", "Add " + type.label.toLowerCase() + " category");
      add.addEventListener("click", () => openCategory(kind));
      header.append(add);
    }
    const list = element("div", "category-rows");
    const content = rows(null, 0, kind);
    list.append(...(content.length ? content : [element("p", "", "No categories yet.")]));
    card.append(header, list);
    return card;
  }));
}

function updateLedgerOptions() {
  if (!snapshot) return;
  const account = filters.elements.account;
  const category = filters.elements.category;
  const categoryMap = new Map(snapshot.categories.map(item => [item.id, item]));
  const kind = filters.elements.kind.value;
  options(account, snapshot.accounts.map(item => [item.id, item.name]), undefined, "All accounts");
  options(category, [["uncategorized", "Uncategorized"], ...snapshot.categories
    .filter(item => !kind || item.kind === kind).map(item => [item.id, categoryPath(item, categoryMap)])],
    undefined, "All categories");
}

function resetLedgerFilters() {
  for (const input of filters.elements) if (input.name) input.value = "";
  ledgerPage = 1;
  ledgerPeriod = "";
  ledgerMonth = "";
  ledgerDescendants = false;
}

async function loadTransactions() {
  if (!snapshot || activeView !== "transactions") return;
  ledgerRequest?.abort();
  const request = new AbortController();
  ledgerRequest = request;
  const params = new URLSearchParams({ page: ledgerPage, size, sample: snapshot.sample ? "1" : "0" });
  for (const [key, value] of new FormData(filters)) if (value.trim()) params.set(key, value.trim());
  if (ledgerPeriod) params.set("period", ledgerPeriod);
  if (ledgerMonth) params.set("month", ledgerMonth);
  if (ledgerDescendants) params.set("descendants", "1");
  $("#clear-ledger-filters").hidden = !["search", "kind", "account", "category", "period", "month"].some(key => params.has(key));
  const range = $("#ledger-period");
  range.hidden = !ledgerPeriod && !ledgerMonth;
  range.textContent = ledgerMonth ? dateLabel(ledgerMonth + "-01", { month: "long", year: "numeric" })
    : ledgerPeriod === period ? dateRange(snapshot.from, snapshot.to)
    : ledgerPeriod === "ALL" ? "All time" : "Period: " + ledgerPeriod;
  if (ledgerDescendants && params.has("category")) range.append(" · Includes subcategories");
  const list = $("#transaction-results");
  try {
    const result = await api("/api/transactions?" + params, { signal: request.signal });
    if (request.signal.aborted) return;
    const pages = Math.max(1, Math.ceil(result.total / size));
    if (ledgerPage > pages) {
      ledgerPage = pages;
      return loadTransactions();
    }
    list.replaceChildren(result.entries.length ? transactionTable(result.entries, true)
      : empty("transactions", "No transactions found", "Add a transaction or change the filters."));
    $("#ledger-pagination").hidden = pages < 2;
    $("#ledger-page-status").textContent = "Page " + ledgerPage + " of " + pages;
    $("#ledger-prev").disabled = ledgerPage <= 1;
    $("#ledger-next").disabled = ledgerPage >= pages;
  } catch (error) {
    if (!request.signal.aborted) {
      list.replaceChildren(element("p", "notice", apiMessage(error)));
      $("#ledger-pagination").hidden = true;
    }
  }
}

function showTransactions({ kind, category, account, period: narrowedPeriod, month, descendants = false }) {
  resetLedgerFilters();
  filters.elements.kind.value = kind ?? "";
  updateLedgerOptions();
  filters.elements.category.value = category ?? "";
  filters.elements.account.value = account ?? "";
  ledgerPeriod = narrowedPeriod ?? "";
  ledgerMonth = month ?? "";
  ledgerDescendants = descendants;
  if (activeView === "transactions") loadTransactions();
  else location.hash = "transactions";
}

function renderSnapshot() {
  const text = dateRange(snapshot.from, snapshot.to);
  $("#overview-period").replaceChildren(icon("calendar"), text);
  $("#flow-period").textContent = text;
  for (const control of $$("[data-period]")) control.setAttribute("aria-pressed", control.dataset.period === period);
  updateHeader();
  updateMetrics();
  updateAllocation();
  updateRecent();
  updateAccounts();
  updateCategories();
  updateLedgerOptions();
  chart.update(snapshot);
  map.update(snapshot, period);
  insights.update(snapshot);
  replay.update(snapshot);
  loadTransactions();
  updateAttention();
}

async function openTransaction(kind = "expense", category) {
  const wasSample = snapshot?.sample;
  if (!await useRealData()) return;
  const defaults = { kind };
  if (category && !wasSample) {
    defaults.category_id = category;
    const owner = realLedger.categories.find(item => item.id === category)?.profile_id;
    defaults.account_id = realLedger.accounts.find(item => item.profile_id === owner)?.id;
  }
  editor.transaction(null, defaults);
}

function editTransaction(entry) {
  if (!mayOpenProfile(entry.profile_id)) return;
  if (entry.profile_id) location.assign(workspaceHref(entry.profile_id, entry.shared ? "shared" : "transactions"));
  else if (entry.shared) location.hash = "shared";
  else editor.transaction(entry);
}

async function openAccount(defaults = {}) {
  if (await useRealData()) editor.account(null, defaults);
}

async function openCategory(kind = "expense", parentId) {
  const wasSample = snapshot?.sample;
  if (await useRealData()) editor.category(null, { kind, parent_id: wasSample ? null : parentId });
}

async function addStarterCategories(event) {
  const control = event.currentTarget;
  control.disabled = true;
  try {
    if (!await useRealData()) return;
    const result = await api("/api/categories/starter", { method: "POST", data: {} });
    await refreshAfterLedgerChange();
    notify(result.added ? result.added + (result.added === 1 ? " starter category added." : " starter categories added.")
      : "Starter categories are already available.");
  } catch (error) {
    notify(apiMessage(error));
  } finally {
    control.disabled = false;
  }
}

function openFunding(prefill) {
  if (activeView === "funding") {
    funding.open(prefill);
    return;
  }
  fundingPrefill = prefill;
  location.hash = "funding";
}

async function openDemo(view = "overview", role = "personal") {
  demoRequest ??= api("/api/workspaces/demo", { method: "POST", data: {} });
  try {
    const demo = await demoRequest;
    location.assign(workspaceHref(demo[role + "_id"] ?? demo.personal_id, view));
  } finally {
    demoRequest = null;
  }
}

function navigate() {
  if (!ready) return;
  const hash = location.hash.slice(1);
  const actual = /^forecast\/actual\/([1-9][0-9]*)$/.exec(hash);
  const runway = /^forecast\/runway\/([0-9]{4}-[0-9]{2}-[0-9]{2})$/.exec(hash);
  const instrument = /^returns\/account\/(-?[1-9][0-9]*)$/.exec(hash);
  const article = /^documentation\/([a-z0-9-]+)$/.exec(hash);
  const allowed = Object.hasOwn(titles, hash) && (hash !== "access" || authState.is_admin);
  const view = actual || runway ? "forecast" : instrument ? "returns" : article ? "documentation" : allowed ? hash : "overview";
  if (hash === activeHash) return;
  activeHash = hash;
  if (view !== "flow" && view !== "overview") map.exitFullscreen();
  activeView = view;
  document.title = titles[view] + " · Quant Financial Planner";
  for (const section of $$("[data-view]")) section.hidden = section.dataset.view !== view;
  const [group, sectionViews] = Object.entries(sections).find(([, views]) => views.includes(view));
  for (const link of $$("[data-nav-group]")) {
    if (link.dataset.navGroup === group) link.setAttribute("aria-current", "true");
    else link.removeAttribute("aria-current");
  }
  for (const link of $$("[data-nav]")) {
    link.hidden = !sectionViews.includes(link.dataset.nav) || (link.dataset.nav === "access" && !authState.is_admin);
    if (link.dataset.nav === view || (view === "replay" && link.dataset.nav === "flow")) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const dedicated = view === "flow";
  $(dedicated ? "#standalone-flow" : "#overview-flow").append($("#flow-panel"));
  $(dedicated ? "#flow-controls" : "#overview-controls").append($("#period-filter"));
  if (dedicated || view === "overview") map.draw();
  if (view === "planner") planner.open();
  if (view === "forecast") forecast.open(actual ? "actual" : "runway", actual?.[1] ?? runway?.[1]);
  if (view === "funding") {
    funding.open(fundingPrefill);
    fundingPrefill = {};
  }
  if (view === "documentation") documentation.open(article?.[1]);
  if (view === "decision") decision.open();
  if (view === "imports") imports.open();
  if (view === "goals") goals.open();
  if (view === "returns") returns.open(instrument ? Number(instrument[1]) : undefined);
  if (view === "budgets") budgets.open();
  if (view === "cars") cars.open();
  if (view === "workspaces") workspaces.load();
  if (view === "access") access.open(authState);
  else access.hide();
  if (view === "shared") shared.open();
  if (view === "insights") insights.open();
  if (view === "replay") replay.open();
  $("#data-context").hidden = !snapshot || usesRealWorkspace(view);
  if (view === "transactions") loadTransactions();
  else ledgerRequest?.abort();
  if (view === "accounts") loadAccountReturns();
  else accountReturnRequest?.abort();
  if (view === "overview") updateAttention();
  window.scrollTo(0, 0);
  documentation.onNavigate(view);
  featureHelp.show(view);
}

$("#add-transaction").addEventListener("click", () => openTransaction());
$("#add-account").addEventListener("click", () => openAccount());
$("#add-cash-wallet").addEventListener("click", () => openAccount({ kind: "cash", name: "Cash wallet" }));
$("#add-category").addEventListener("click", () => openCategory());
$("#add-starter-categories").addEventListener("click", addStarterCategories);
$("#retry-connection").addEventListener("click", () => ready ? refresh() : start());
$("#toggle-sample").addEventListener("click", () => switchMode(!(snapshot?.sample ?? false)));
$("#period-filter").addEventListener("click", event => {
  const control = event.target.closest("[data-period]");
  if (!control || control.dataset.period === period) return;
  period = control.dataset.period;
  refresh();
});
$("#ledger-prev").addEventListener("click", () => { ledgerPage -= 1; loadTransactions(); });
$("#ledger-next").addEventListener("click", () => { ledgerPage += 1; loadTransactions(); });
$("#clear-ledger-filters").addEventListener("click", () => { resetLedgerFilters(); updateLedgerOptions(); loadTransactions(); });
filters.addEventListener("submit", event => { event.preventDefault(); clearTimeout(ledgerTimer); ledgerPage = 1; loadTransactions(); });
filters.addEventListener("change", event => {
  if (event.target.name === "kind") updateLedgerOptions();
  if (event.target.name !== "search") { ledgerPage = 1; loadTransactions(); }
});
filters.elements.search.addEventListener("input", () => {
  clearTimeout(ledgerTimer);
  ledgerTimer = setTimeout(() => { ledgerPage = 1; loadTransactions(); }, 180);
});
$(".skip-link").addEventListener("click", event => { event.preventDefault(); $("#main").focus(); });
window.addEventListener("hashchange", navigate);
window.addEventListener("sessionexpired", () => location.replace(signInHref()));
window.addEventListener("pageshow", event => {
  if (event.persisted) location.reload();
});

async function start() {
  const retry = $("#retry-connection");
  retry.disabled = true;
  try {
    authState = await authRequest("state");
    if (!authState.username) {
      location.replace(signInHref());
      return;
    }
    document.documentElement.dataset.profileId = authState.profile_id;
    const active = await workspaces.load();
    if (!active) throw new Error("Workspace is unavailable");
    sampleMode = !active.demo_role && readMode();
    ready = true;
    setupAuthShell({ state: authState, notify });
    navigate();
    await refresh();
    documentation.maybeShowGuide(authState.profile_id);
  } catch (error) {
    $("#connection-message").textContent = apiMessage(error);
    $("#connection-error").hidden = false;
  } finally {
    retry.disabled = false;
  }
}

start();
