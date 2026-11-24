import { $, $$, ApiError, accountTypes, api, apiMessage, button, categoryPath, colors, entryTypes, euros, flows, options } from "./ui.js";
import { mayOpenProfile } from "./workspaces.js";
import { attachmentPanel } from "./attachments.js";

const resources = {
  transaction: "transactions",
  account: "accounts",
  category: "categories",
};

function pairs(items) {
  return Object.entries(items).map(([key, item]) => [key, item.label]);
}

function labelValues(input) {
  return input.split(",").map(label => label.trim()).filter(Boolean);
}

export function ledgerEditor(actions) {
  const dialog = $("#editor");
  const form = $("#editor-form");
  const fields = $("#editor-fields");
  const save = $("#editor-save");
  const remove = $("#editor-delete");
  const issue = $("#editor-error");
  let context;
  let documents;
  let busy = false;

  function read() {
    const data = Object.fromEntries(new FormData(form));
    const statement = form.elements.statement_expected;
    if (statement && !statement.disabled) data.statement_expected = statement.checked;
    return data;
  }

  function setValues(data) {
    for (const [key, value] of Object.entries(data)) {
      const field = form.elements.namedItem(key);
      if (field && value !== null && value !== undefined) {
        if (field.type === "checkbox") field.checked = Boolean(value);
        else field.value = value;
      }
    }
  }

  function populateAccount(values, ledger) {
    const type = form.elements.kind;
    options(type, pairs(accountTypes), values.kind ?? "checking");
    type.disabled = Boolean(context.id);
    form.elements.opened_on.max = ledger.today;
    setValues({ opened_on: ledger.today, opening_balance: "0.00", ...values });
  }

  function populateCategory(values, ledger) {
    const type = form.elements.kind;
    const parents = form.elements.parent_id;
    const choices = form.elements.color;
    const categoryMap = new Map(ledger.categories.map(item => [item.id, item]));
    const forbidden = new Set();
    if (context.id) {
      for (const category of ledger.categories) {
        for (let ancestor = category; ancestor; ancestor = categoryMap.get(ancestor.parent_id)) {
          if (ancestor.id === context.id) forbidden.add(category.id);
        }
      }
    }
    function updateParents(selected) {
      const available = ledger.categories.filter(item => item.kind === type.value && !item.profile_id && !forbidden.has(item.id));
      options(parents, available.map(item => [item.id, categoryPath(item, categoryMap)]), selected, "Top level");
    }
    options(type, pairs(flows), values.kind ?? "expense");
    type.disabled = Boolean(context.id);
    options(choices, Object.entries(colors), values.color ?? flows[type.value].color);
    setValues({ name: values.name });
    updateParents(values.parent_id);
    type.addEventListener("change", () => {
      updateParents("");
      choices.value = flows[type.value].color;
    });
  }

  function populateTransaction(values, ledger) {
    const type = form.elements.kind;
    const account = form.elements.account_id;
    const destination = form.elements.destination_id;
    const category = form.elements.category_id;
    const statement = form.elements.statement_expected;
    const statementOption = $("[data-statement-option]", form);
    const statementHint = $("[data-statement-hint]", form);
    const bank = new Set(["checking", "savings"]);
    let statementOverride = values.statement_expected;
    let previousDefault;
    let statementEdited = false;
    const help = $("[data-kind-help]", form);
    const categoryMap = new Map(ledger.categories.map(item => [item.id, item]));

    function updateRelated(selection) {
      const kind = type.value;
      const hasDestination = kind === "investment" || kind === "transfer";
      const canCategorize = Object.hasOwn(flows, kind);
      const sourceAccounts = ledger.accounts.filter(item => {
        if (!mayOpenProfile(item.profile_id)) return false;
        if (kind === "valuation") return item.kind === "investment";
        if (kind === "investment") return item.kind !== "investment";
        return true;
      });
      const source = sourceAccounts.some(item => String(item.id) === String(selection.account_id))
        ? selection.account_id : sourceAccounts[0]?.id;
      options(account, sourceAccounts.map(item => [item.id, item.name]), source,
        sourceAccounts.length ? "Choose account" : "Add an account first");
      const sourceAccount = sourceAccounts.find(item => String(item.id) === account.value);
      const owner = sourceAccount?.profile_id;
      const targetAccounts = ledger.accounts.filter(item => String(item.id) !== account.value
        && item.profile_id === owner
        && (kind !== "investment" || item.kind === "investment"));
      options(destination, targetAccounts.map(item => [item.id, item.name]), selection.destination_id,
        targetAccounts.length ? "Choose destination" : "Add a destination account");
      const targetAccount = targetAccounts.find(item => String(item.id) === destination.value);
      const expectedByDefault = bank.has(sourceAccount?.kind) || (hasDestination && bank.has(targetAccount?.kind));
      if (!statementEdited && previousDefault !== undefined && previousDefault !== expectedByDefault) {
        statementOverride = undefined;
      }
      previousDefault = expectedByDefault;
      statementOption.hidden = statement.disabled = kind === "valuation";
      statement.checked = statementOverride ?? expectedByDefault;
      statementHint.textContent = expectedByDefault ? "Quant can match it when you import this account’s statement."
        : "Leave unchecked for purchases paid from your cash wallet.";
      const categories = ledger.categories.filter(item => item.kind === kind && item.profile_id === owner);
      options(category, categories.map(item => [item.id, categoryPath(item, categoryMap)]), selection.category_id, "Uncategorized");
      $("[data-account-label]", form).textContent = hasDestination ? "Source account" : "Account";
      $("[data-amount-label]", form).textContent = kind === "valuation" ? "Value change (€)" : "Amount (€)";
      $("[data-destination]", form).hidden = !hasDestination;
      $("[data-category-field]", form).hidden = !canCategorize;
      $("[data-new-category]", form).hidden = !canCategorize || Boolean(owner);
      destination.disabled = !hasDestination;
      category.disabled = !canCategorize;
      const notes = {
        investment: "Moves cash to an investment account and records a contribution.",
        transfer: "Moves money between your accounts without changing total wealth.",
        valuation: "Enter the increase or decrease since the last recorded investment value.",
      };
      help.textContent = notes[kind] ?? "";
      help.hidden = !help.textContent;
    }

    options(type, pairs(entryTypes), values.kind ?? "expense");
    form.elements.posted_on.max = ledger.today;
    setValues({ posted_on: ledger.today, ...values });
    const remembered = context.id ? {} : ledger.defaults[type.value] ?? {};
    updateRelated({ ...remembered, ...values });
    type.addEventListener("change", () => updateRelated(context.id ? read() : ledger.defaults[type.value] ?? {}));
    account.addEventListener("change", () => updateRelated(read()));
    destination.addEventListener("change", () => updateRelated(read()));
    statement.addEventListener("change", () => {
      statementOverride = statement.checked;
      statementEdited = true;
    });

    const labels = form.elements.labels;
    const suggestions = $(".label-suggestions", form);
    for (const label of ledger.labels.slice(0, 8)) {
      const chip = button(label, "label-chip");
      chip.addEventListener("click", () => {
        const existing = labelValues(labels.value);
        if (!existing.some(value => value.toLowerCase() === label.toLowerCase()) && existing.length < 12) {
          labels.value = [...existing, label].join(", ");
        }
      });
      suggestions.append(chip);
    }
    suggestions.hidden = !suggestions.children.length;

    $("[data-new-account]", form).addEventListener("click", () => {
      const original = context;
      const draft = read();
      const needsInvestment = ["valuation", "investment"].includes(type.value)
        && !ledger.accounts.some(item => item.kind === "investment");
      const kind = needsInvestment ? "investment" : "checking";
      open("account", {
        data: { kind },
        cancel: () => open("transaction", { ...original, data: draft }),
        after: id => {
          const next = { ...draft };
          if (draft.kind === "investment" && kind === "investment") next.destination_id = id;
          else if (!draft.account_id) next.account_id = id;
          else if (!draft.destination_id && ["investment", "transfer"].includes(draft.kind)) next.destination_id = id;
          open("transaction", { ...original, data: next });
        },
      });
    });
    $("[data-new-category]", form).addEventListener("click", () => {
      const original = context;
      const draft = read();
      open("category", {
        data: { kind: draft.kind },
        cancel: () => open("transaction", { ...original, data: draft }),
        after: id => open("transaction", { ...original, data: { ...draft, category_id: id } }),
      });
    });
  }

  function setBusy(next) {
    busy = next;
    for (const control of [save, remove, ...$$("[data-cancel]", form)]) control.disabled = busy;
    documents?.setBusy(busy);
  }

  function showIssue(error) {
    issue.textContent = apiMessage(error);
    issue.hidden = false;
    const field = error.field && form.elements.namedItem(error.field);
    if (field) {
      field.setAttribute("aria-invalid", "true");
      field.setAttribute("aria-describedby", "editor-error");
      field.focus();
    }
  }

  function clearIssue() {
    issue.hidden = true;
    for (const field of $$("[aria-invalid]", form)) {
      field.removeAttribute("aria-invalid");
      field.removeAttribute("aria-describedby");
    }
  }

  function open(resource, config = {}) {
    const ledger = actions.ledger();
    if (!ledger) return;
    const { id, data = {}, description = "", after, cancel, requestKey, pending = [] } = config;
    context = { resource, id, after, cancel, pending,
      requestKey: requestKey ?? (resource === "transaction" ? crypto.randomUUID() : null) };
    $("#editor-title").textContent = (id ? "Edit " : "Add ") + resource;
    const subtitle = $("#editor-description");
    subtitle.textContent = description;
    subtitle.hidden = !description;
    documents?.stop();
    documents = null;
    fields.replaceChildren($("#" + resource + "-fields").content.cloneNode(true));
    clearIssue();
    remove.hidden = !id;
    save.textContent = id ? "Save changes" : "Save " + resource;
    if (resource === "transaction") {
      populateTransaction(data, ledger);
      documents = attachmentPanel($("[data-transaction-attachments]", fields), {
        pending, target: id ? { id } : null, notify: actions.notify, onChange: actions.refresh, onBusy: setBusy,
      });
    }
    if (resource === "account") populateAccount(data, ledger);
    if (resource === "category") populateCategory(data, ledger);
    if (!dialog.open) dialog.showModal();
    form.elements.name.focus();
  }

  function transaction(entry = null, defaults = {}) {
    const ledger = actions.ledger();
    if (!entry && !ledger.accounts.length) {
      open("account", {
        description: "Add the account this transaction belongs to.",
        after: id => open("transaction", { data: { ...defaults, account_id: id } }),
      });
      return;
    }
    const data = entry ? { ...entry, amount: euros(entry.amount), labels: entry.labels.join(", ") } : defaults;
    open("transaction", { id: entry?.id, data });
  }

  function cancel() {
    if (busy) return;
    if (context?.cancel) context.cancel();
    else dialog.close();
  }

  async function commit(method) {
    if (busy) return;
    if (context.retryAttachments) {
      clearIssue();
      setBusy(true);
      try {
        if (await uploadSaved(context.retryAttachments)) {
          dialog.close();
          actions.notify("Transaction and attachments saved.");
          await actions.refresh();
        }
      } finally {
        setBusy(false);
      }
      return;
    }
    const { resource, id, after, requestKey } = context;
    const path = "/api/" + resources[resource] + (id ? "/" + id : "");
    const data = method === "DELETE" ? undefined : read();
    if (resource === "transaction" && data) {
      data.labels = labelValues(data.labels);
      data.request_key = requestKey;
    }
    const deleting = method === "DELETE";
    if (deleting && !window.confirm("Delete this " + resource + "?")) return;
    clearIssue();
    setBusy(true);
    let result;
    try {
      result = await api(path, { method, data });
    } catch (error) {
      showIssue(error);
      setBusy(false);
      return;
    }
    if (!deleting && resource === "transaction" && context.pending.length) {
      const target = result.attachment_target ?? { id: result.id };
      if (!await uploadSaved(target)) {
        setBusy(false);
        await actions.refresh();
        return;
      }
    }
    if (!after || deleting) dialog.close();
    if (resource === "category") actions.onCategoryChange?.();
    actions.notify((resource[0].toUpperCase() + resource.slice(1)) + (deleting ? " deleted." : " saved."));
    try {
      const refreshed = await actions.refresh();
      if (after && !deleting) {
        if (refreshed) after(result.id);
        else {
          dialog.close();
          actions.notify("Saved. Reload the dashboard to continue editing.");
        }
      }
    } finally {
      setBusy(false);
    }
  }

  async function uploadSaved(target) {
    try {
      await documents.upload(target);
      return true;
    } catch (error) {
      context.retryAttachments = target;
      $("#editor-title").textContent = "Transaction saved";
      save.textContent = "Retry attachments";
      remove.hidden = true;
      const panel = $("[data-transaction-attachments]", fields);
      for (const control of $$("input,select,textarea,button", fields)) {
        if (!panel.contains(control)) control.disabled = true;
      }
      showIssue(new ApiError("The transaction is saved. " + apiMessage(error), null, error.status));
      return false;
    }
  }

  form.addEventListener("submit", event => {
    event.preventDefault();
    commit(context.id ? "PUT" : "POST");
  });
  for (const control of $$("[data-cancel]", form)) control.addEventListener("click", cancel);
  dialog.addEventListener("cancel", event => {
    event.preventDefault();
    cancel();
  });
  dialog.addEventListener("close", () => {
    if (!dialog.open) {
      documents?.stop();
      documents = null;
    }
  });
  remove.addEventListener("click", () => commit("DELETE"));
  form.addEventListener("input", () => { if (!issue.hidden) clearIssue(); });

  return {
    transaction,
    account: (entry, defaults = {}) => open("account", {
      id: entry?.id, data: entry ? { ...entry, opening_balance: euros(entry.opening_balance) } : defaults,
    }),
    category: (entry, defaults = {}) => open("category", { id: entry?.id, data: entry ?? defaults }),
  };
}
