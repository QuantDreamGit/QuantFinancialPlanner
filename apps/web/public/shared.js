import { $, api, apiMessage, button, categoryPath, dateLabel, element, empty, euros, money, options } from "./ui.js";

const endpoint = "/api/shared";
const frequencyNames = { once: "One time", weekly: "Weekly", monthly: "Monthly", yearly: "Yearly" };

function choiceField(label, name, choices, selected, prompt) {
  const input = element("select");
  input.name = name;
  options(input, choices, selected, prompt);
  return [fieldWrap(label, input), input];
}

function entryField(label, name, { value = "", type = "text", required = true, max, limit, inputMode } = {}) {
  const input = element("input");
  input.type = type;
  input.name = name;
  input.value = value;
  input.required = required;
  if (max) input.max = max;
  if (limit) input.maxLength = limit;
  if (inputMode) input.inputMode = inputMode;
  return [fieldWrap(label, input), input];
}

function fieldWrap(label, input) {
  const wrapper = element("label", "field");
  wrapper.append(element("span", "", label), input);
  return wrapper;
}

function panel(heading, action) {
  const node = element("section", "panel shared-panel");
  const title = element("div", "panel-heading");
  title.append(element("h2", "", heading));
  if (action) title.append(action);
  node.append(title);
  return node;
}

function action(label, callback, className = "secondary", iconName = "") {
  const node = button(label, className, iconName);
  node.addEventListener("click", callback);
  return node;
}

function checkbox(label, checked) {
  const wrapper = element("label", "shared-choice");
  const input = element("input");
  input.type = "checkbox";
  input.checked = checked;
  wrapper.append(input, element("span", "", label));
  return [wrapper, input];
}

function paymentAmounts(gross, personal, payer) {
  const yours = BigInt(personal);
  const owed = payer ? yours : BigInt(gross) - yours;
  const values = [
    [payer ? "Paid by " + payer : "Cash paid", gross],
    ["Your share", personal],
    [payer ? "You owe" : "Owed back", owed],
  ];
  const amounts = element("dl", "shared-payment-amounts");
  for (const [name, value] of values) {
    const item = element("div");
    item.append(element("dt", "muted", name), element("dd", "", money(value, { fixed: true })));
    amounts.append(item);
  }
  return amounts;
}

function settlementMovement(amount) {
  const cents = BigInt(amount);
  return (cents > 0n ? "Cash received " : "Cash repaid ") + money(cents > 0n ? cents : -cents, { fixed: true });
}

export function setupShared(root, { onChange = () => {} } = {}) {
  const main = element("div", "shared-main");
  const title = element("div", "page-heading");
  const titleCopy = element("div");
  const titleText = element("h1", "", "Shared payments");
  titleText.id = "shared-heading";
  titleCopy.append(titleText, element("p", "page-description", "Split one-off purchases and recurring bills with the people you tag."));
  const addPayment = action("Add shared payment", () => openRule(), "primary", "plus");
  addPayment.disabled = true;
  title.append(titleCopy, addPayment);

  const notification = element("p", "notice");
  notification.setAttribute("role", "alert");
  notification.hidden = true;
  const dialog = element("dialog", "editor");
  dialog.setAttribute("aria-labelledby", "shared-dialog-title");
  const form = element("form");
  const heading = element("h2");
  heading.id = "shared-dialog-title";
  const header = element("div", "dialog-heading");
  const fields = element("div", "shared-fields");
  const formNotice = element("p", "notice");
  formNotice.setAttribute("role", "alert");
  formNotice.hidden = true;
  const controls = element("div", "dialog-actions");
  const close = action("Cancel", () => dialog.close());
  const save = element("button", "primary", "Save");
  save.type = "submit";
  header.append(heading);
  controls.append(close, save);
  form.append(header, fields, formNotice, controls);
  dialog.append(form);
  root.replaceChildren(title, notification, main, dialog);

  let state;
  let request;
  let busy = false;
  let commit;
  let topHistory = 20;

  function personName(person) {
    return person.name + " (@" + person.tag + ")";
  }

  function findAccount(key) {
    return state.accounts.find(account => account.id === key);
  }

  function findPerson(key) {
    return state.people.find(person => person.id === key);
  }

  function personChoices() {
    return state.people.map(person => [person.id, personName(person)]);
  }

  function accountChoices() {
    return state.accounts.map(account => [account.id, account.name]);
  }

  async function refresh() {
    request?.abort();
    const next = new AbortController();
    request = next;
    try {
      const result = await api(endpoint, { signal: next.signal });
      if (next.signal.aborted) return;
      state = result;
      notification.hidden = true;
      render();
    } catch (error) {
      if (next.signal.aborted) return;
      notification.replaceChildren(apiMessage(error), " ", action("Retry", refresh));
      notification.hidden = false;
    }
  }

  function openForm(label, definitions, saveLabel, submit) {
    heading.textContent = label;
    fields.replaceChildren(...definitions);
    formNotice.hidden = true;
    for (const input of form.querySelectorAll("[aria-invalid]")) input.removeAttribute("aria-invalid");
    save.textContent = saveLabel;
    commit = submit;
    if (!dialog.open) dialog.showModal();
    fields.querySelector("input, select, button")?.focus();
  }

  async function mutate(path, data, method = "POST") {
    await api(endpoint + path, { method, data });
    if (dialog.open) dialog.close();
    await refresh();
    onChange();
  }

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !commit) return;
    busy = close.disabled = save.disabled = true;
    formNotice.hidden = true;
    for (const input of form.querySelectorAll("[aria-invalid]")) input.removeAttribute("aria-invalid");
    try {
      await commit();
    } catch (error) {
      notification.hidden = true;
      formNotice.textContent = apiMessage(error);
      formNotice.hidden = false;
      const invalid = error.field && form.elements.namedItem(error.field);
      if (invalid instanceof HTMLElement) {
        invalid.setAttribute("aria-invalid", "true");
        invalid.focus();
      }
    } finally {
      busy = close.disabled = save.disabled = false;
    }
  });
  dialog.addEventListener("cancel", event => { if (busy) event.preventDefault(); });

  function openPerson(person, after) {
    const [nameLabel, name] = entryField("Name", "name", { value: person?.name, limit: 60 });
    const [tagLabel, tag] = entryField("Tag", "tag", { value: person ? "@" + person.tag : "", limit: 32 });
    tag.placeholder = "@name";
    openForm(person ? "Edit person" : "Add person", [nameLabel, tagLabel], "Save person", async () => {
      await mutate("/people" + (person ? "/" + person.id : ""), { name: name.value, tag: tag.value }, person ? "PUT" : "POST");
      if (after) after();
    });
  }

  function openRule(rule) {
    if (!state.people.length) {
      openPerson(null, () => openRule());
      return;
    }
    const stableKey = crypto.randomUUID();
    const grid = element("div", "field-grid");
    const [nameLabel, name] = entryField("Payment name", "name", { value: rule?.name, limit: 100 });
    nameLabel.classList.add("full");
    const [amountLabel, amount] = entryField("Total paid (€)", "amount", {
      value: rule ? euros(rule.amount) : "", inputMode: "decimal",
    });
    const [frequencyLabel, frequency] = choiceField("Frequency", "frequency", Object.entries(frequencyNames), rule?.frequency ?? "once");
    const [dateField, first] = entryField("First due date", "first_on", { type: "date", value: rule?.first_on ?? state.today });
    const [endLabel, end] = entryField("Last allowed date (optional)", "end_on", { type: "date", value: rule?.end_on ?? "", required: false });
    const [payerLabel, payer] = choiceField("Paid by", "payer_id", [["", "Me"], ...personChoices()], rule?.payer_id ?? "");
    const [accountLabel, account] = choiceField("Account paying the bill", "account_id", accountChoices(),
      rule?.account_id ?? state.accounts[0]?.id, "Select account");
    const categories = new Map(state.categories.map(category => [category.id, category]));
    const [categoryLabel, category] = choiceField("Expense category", "category_id",
      state.categories.map(item => [item.id, categoryPath(item, categories)]), rule?.category_id ?? "", "Uncategorized");
    const [splitLabel, split] = choiceField("Split method", "split", [["equal", "Equal shares"], ["custom", "Exact amounts"]], rule?.split ?? "equal");
    const [labelsLabel, labels] = entryField("Labels (optional)", "labels", {
      value: rule?.labels.join(", "), limit: 450, required: false,
    });
    labels.placeholder = "e.g. flat, streaming";
    const [noteLabel, note] = entryField("Note (optional)", "note", { value: rule?.note, limit: 600, required: false });
    noteLabel.classList.add("full");
    grid.append(nameLabel, amountLabel, frequencyLabel, dateField, endLabel, payerLabel, accountLabel,
      categoryLabel, splitLabel, labelsLabel, noteLabel);

    const pickMembers = element("fieldset", "shared-members");
    pickMembers.append(element("legend", "", "Split between you and"));
    const portions = new Map((rule?.shares ?? []).map(part => [String(part.person_id ?? "me"), euros(part.amount)]));
    const assigned = [];
    const [myShareLabel, myShare] = entryField("Your share (€)", "share-me", { value: portions.get("me"), inputMode: "decimal" });
    pickMembers.append(myShareLabel);
    for (const person of state.people) {
      const [checkLabel, check] = checkbox(personName(person), portions.has(String(person.id)));
      const [shareLabel, share] = entryField("Share for " + person.name + " (€)", "share-" + person.id, {
        value: portions.get(String(person.id)), inputMode: "decimal",
      });
      const row = element("div", "shared-member");
      row.append(checkLabel, shareLabel);
      pickMembers.append(row);
      assigned.push({ id: person.id, check, share, shareLabel });
    }
    const [firstRecordLabel, record] = checkbox("Record the first payment now", !rule);
    const explanation = element("p", "muted shared-form-note");
    function sync() {
      const outsidePayer = payer.value !== "";
      accountLabel.hidden = outsidePayer;
      account.required = !outsidePayer;
      endLabel.hidden = frequency.value === "once";
      end.disabled = endLabel.hidden;
      firstRecordLabel.hidden = Boolean(rule) || !first.value || first.value > state.today;
      myShareLabel.hidden = myShare.disabled = split.value !== "custom";
      for (const { check, share, shareLabel } of assigned) {
        shareLabel.hidden = share.disabled = !check.checked || split.value !== "custom";
      }
      explanation.textContent = outsidePayer
        ? "Your share counts as spending and becomes the amount you owe. Record the repayment when cash leaves your account."
        : "Cash paid is the full amount leaving the account. Your share counts as spending. The remaining amount is owed back.";
      if (!outsidePayer && !state.accounts.length) {
        explanation.textContent = "Add a checking, savings or cash account before recording a payment you made.";
      }
    }
    for (const control of [frequency, payer, first, split, ...assigned.map(item => item.check)]) {
      control.addEventListener("change", sync);
    }
    sync();
    openForm(rule ? "Edit shared payment" : "Add shared payment", [grid, pickMembers, firstRecordLabel, explanation],
      "Save payment", async () => {
        const selected = assigned.filter(item => item.check.checked);
        const shares = Object.fromEntries([["me", myShare.value], ...selected.map(item => [item.id, item.share.value])]);
        await mutate("/rules" + (rule ? "/" + rule.id : ""), {
          name: name.value, amount: amount.value, frequency: frequency.value, first_on: first.value,
          end_on: end.disabled ? "" : end.value, payer_id: payer.value, account_id: account.value, category_id: category.value,
          split: split.value, labels: labels.value.split(",").map(value => value.trim()).filter(Boolean), note: note.value,
          participants: selected.map(item => item.id), shares, record_now: !firstRecordLabel.hidden && record.checked,
          request_key: stableKey,
        }, rule ? "PUT" : "POST");
      });
  }

  function openOccurrence(rule, skip) {
    const [dueLabel, due] = choiceField("Scheduled date", "due_on",
      rule.pending.map(day => [day, dateLabel(day)]));
    const [paidLabel, paid] = entryField("Actual payment date", "posted_on", {
      type: "date", value: rule.pending[0] < state.today ? rule.pending[0] : state.today, max: state.today,
    });
    const account = findAccount(rule.account_id);
    if (account) paid.min = account.opened_on;
    due.addEventListener("change", () => { paid.value = due.value < state.today ? due.value : state.today; });
    openForm(skip ? "Skip " + rule.name : "Record " + rule.name,
      skip ? [dueLabel] : [dueLabel, paidLabel], skip ? "Skip occurrence" : "Record payment", async () => {
        await mutate("/rules/" + rule.id + "/" + (skip ? "skip" : "record"), { due_on: due.value, posted_on: paid.value });
      });
  }

  function schedulable(person) {
    return (state.expected ?? []).filter(plan => plan.person_id === person.id && plan.funded)
      .reduce((left, plan) => left - BigInt(plan.signed_amount), BigInt(person.balance));
  }

  function openSettlement(person, planned = false) {
    const stableKey = crypto.randomUUID();
    const balance = planned ? schedulable(person) : BigInt(person.balance);
    const receiving = balance > 0n;
    const [amountLabel, amount] = entryField("Amount (€)", "amount", {
      value: euros(receiving ? balance : -balance), inputMode: "decimal",
    });
    const [accountLabel, account] = choiceField(receiving ? "Deposit into" : "Pay from", "account_id", accountChoices(),
      state.accounts[0]?.id, "Choose account");
    const [postedLabel, posted] = entryField(planned ? "Expected date" : "Payment date", planned ? "due_on" : "posted_on",
      { type: "date", value: state.today, max: planned ? undefined : state.today });
    if (planned) posted.min = state.today;
    const [noteLabel, note] = entryField("Note (optional)", "note", { required: false, limit: 600 });
    const grid = element("div", "field-grid");
    noteLabel.classList.add("full");
    grid.append(amountLabel, accountLabel, postedLabel, noteLabel);
    openForm((planned ? "Schedule " : "") + (receiving ? "reimbursement from " : "repayment to ") + person.name,
      [grid], planned ? "Schedule settlement" : "Record settlement",
      async () => {
        await mutate(planned ? "/expected" : "/settlements", {
          person_id: person.id, direction: receiving ? "receive" : "pay", amount: amount.value,
          account_id: account.value, [posted.name]: posted.value, note: note.value, request_key: stableKey,
        });
      });
  }

  function expectedPanel() {
    const panelBox = panel("Scheduled settlements");
    for (const plan of state.expected) {
      const receiving = BigInt(plan.signed_amount) > 0n;
      const name = (receiving ? "Reimbursement from " : "Repayment to ") + "@" + plan.tag;
      const row = element("article", "shared-row");
      const text = element("div", "shared-row-text");
      text.append(element("h3", "", name), element("p", "muted",
        [dateLabel(plan.due_on), "Scheduled " + settlementMovement(plan.signed_amount).toLowerCase(), findAccount(plan.account_id)?.name,
          plan.funded ? null : "Exceeds current balance"].filter(Boolean).join(" · ")));
      const actions = element("div", "shared-row-actions");
      if (plan.funded) actions.append(action("Record", () => {
        const [label, posted] = entryField("Actual payment date", "posted_on", {
          type: "date", value: plan.due_on < state.today ? plan.due_on : state.today, max: state.today,
        });
        posted.min = findAccount(plan.account_id)?.opened_on ?? "";
        openForm(name, [label], "Record settlement",
          () => mutate("/expected/" + plan.id + "/record", { posted_on: posted.value }));
      }));
      actions.append(action("Cancel", () => openForm("Cancel scheduled settlement",
        [element("p", "muted", "Cancel the forecast for " + name.toLowerCase() + "?")], "Cancel settlement",
        () => mutate("/expected/" + plan.id + "/cancel", {})), "text-button"));
      row.append(text, actions);
      panelBox.append(row);
    }
    return panelBox;
  }

  function settleBalances() {
    const grid = element("div", "shared-summary");
    for (const [label, cents] of [
      ["Owed back to you", state.owed], ["You owe", state.owe], ["Net shared balance", BigInt(state.owed) - BigInt(state.owe)],
    ]) {
      const item = element("section", "panel metric");
      item.append(element("h2", "", label), element("p", "metric-value", money(cents, { fixed: true })));
      grid.append(item);
    }
    return grid;
  }

  function peoplePanel() {
    const panelBox = panel("People", action("Add person", () => openPerson(), "secondary", "plus"));
    if (!state.people.length) {
      panelBox.append(empty("tags", "No tagged people", "Add the people you split payments with."));
      return panelBox;
    }
    for (const person of state.people) {
      const value = BigInt(person.balance);
      const row = element("article", "shared-row");
      const text = element("div", "shared-row-text");
      const balance = value > 0n ? "Owed back " + money(value) : value < 0n ? "You owe " + money(-value) : "Settled up";
      text.append(element("h3", "", person.name), element("p", "muted", "@" + person.tag + " · " + balance));
      const actions = element("div", "shared-row-actions");
      if (value !== 0n) actions.append(action("Settle", () => openSettlement(person)));
      if (schedulable(person) !== 0n) actions.append(action("Schedule", () => openSettlement(person, true), "text-button"));
      actions.append(action("Edit", () => openPerson(person), "text-button"));
      row.append(text, actions);
      panelBox.append(row);
    }
    return panelBox;
  }

  function schedulePanel() {
    const panelBox = panel("Payment schedules");
    if (!state.rules.length) {
      panelBox.append(empty("calendar", "No shared payments yet", "Start with a bill or a purchase that you split."));
      return panelBox;
    }
    for (const rule of state.rules) {
      const row = element("article", "shared-row");
      const text = element("div", "shared-row-text");
      const payer = rule.payer_id ? findPerson(rule.payer_id)?.name ?? "another person" : null;
      const nextDue = rule.pending[0];
      const status = rule.stopped_at ? "Ended" : nextDue
        ? (nextDue < state.today ? "Due " : "Next ") + dateLabel(nextDue)
        : "No pending occurrences";
      const account = findAccount(rule.account_id)?.name;
      text.append(element("h3", "", rule.name), element("p", "muted", [frequencyNames[rule.frequency], status, account]
        .filter(Boolean).join(" · ")), paymentAmounts(rule.amount, rule.shares[0].amount, payer));
      const tags = element("div", "shared-tags");
      for (const share of rule.shares.slice(1)) {
        const person = findPerson(share.person_id);
        if (person) tags.append(element("span", "label-chip", "@" + person.tag + " " + money(share.amount)));
      }
      if (tags.childElementCount) text.append(tags);
      const actions = element("div", "shared-row-actions");
      if (nextDue && !rule.stopped_at) {
        actions.append(action("Record", () => openOccurrence(rule, false), "secondary"));
        actions.append(action("Skip", () => openOccurrence(rule, true), "text-button"));
      }
      if (!rule.stopped_at && (nextDue || rule.frequency !== "once")) {
        actions.append(action("Edit", () => openRule(rule), "text-button"));
        actions.append(action("End", () => openForm("End " + rule.name,
          [element("p", "muted", "This ends future occurrences and keeps the payments you recorded.")], "End schedule",
          () => mutate("/rules/" + rule.id + "/stop", {})), "text-button"));
      }
      row.append(text, actions);
      panelBox.append(row);
    }
    return panelBox;
  }

  function postedPanel() {
    const panelBox = panel("Payment history");
    if (!state.history.length) {
      panelBox.append(empty("transactions", "No recorded activity", "Recorded payments and reimbursements appear here."));
      return panelBox;
    }
    for (const entry of state.history.slice(0, topHistory)) {
      const row = element("article", "shared-row");
      const text = element("div", "shared-row-text");
      const account = findAccount(entry.account_id)?.name;
      const person = entry.type === "settlement" ? "@" + entry.tag : "";
      const inCash = entry.type === "settlement" && BigInt(entry.signed_amount) > 0n;
      const label = entry.type === "payment" ? entry.name : (inCash ? "Reimbursement from " : "Repayment to ") + person;
      const movement = entry.type === "settlement" ? settlementMovement(entry.signed_amount) : null;
      text.append(element("h3", "", label), element("p", "muted", [dateLabel(entry.posted_on), movement,
        account, entry.voided_at ? "Voided" : null].filter(Boolean).join(" · ")));
      if (entry.type === "payment") {
        const payer = entry.payer_id ? findPerson(entry.payer_id)?.name ?? "another person" : null;
        text.append(paymentAmounts(entry.gross, entry.personal, payer));
      }
      if (entry.type === "payment" && entry.labels.length) {
        const cluster = element("div", "shared-tags");
        cluster.append(...entry.labels.map(label => element("span", "label-chip", label)));
        text.append(cluster);
      }
      if (entry.type === "payment" && entry.shares?.length) {
        const breakdown = element("details", "shared-breakdown");
        const items = element("div", "shared-tags");
        items.append(...entry.shares.map(share => element("span", "label-chip", share.member + " " + money(share.amount))));
        breakdown.append(element("summary", "", "Recorded shares"), items);
        text.append(breakdown);
      }
      row.append(text);
      if (!entry.voided_at) {
        row.append(action("Void", () => openForm("Void " + label,
          [element("p", "muted", "This removes the entry from balances and keeps a record here.")], "Void entry",
          () => mutate((entry.type === "payment" ? "/postings/" : "/settlements/") + entry.id + "/void", {})), "text-button"));
      }
      panelBox.append(row);
    }
    if (topHistory < state.history.length) {
      panelBox.append(action("Show earlier activity", () => { topHistory += 20; render(); }, "text-button"));
    }
    return panelBox;
  }

  function render() {
    addPayment.disabled = false;
    const members = state.members?.length ? element("section", "panel shared-panel") : null;
    if (members) {
      members.append(element("h2", "", "Member shared balances"));
      for (const member of state.members) {
        const line = element("div", "shared-row");
        const open = element("a", "text-link", member.name);
        open.href = "/?workspace=" + member.id + "#shared";
        line.append(open, element("span", "muted", "Owed back " + money(member.owed) + " · Owes " + money(member.owe)
          + (member.overdue ? " · " + member.overdue + " overdue" : "")));
        members.append(line);
      }
    }
    main.replaceChildren(...(members ? [members] : []), settleBalances(), peoplePanel(), schedulePanel(),
      ...(state.expected?.length ? [expectedPanel()] : []), postedPanel());
  }

  return { open: refresh };
}
