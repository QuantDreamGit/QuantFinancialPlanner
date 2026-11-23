import { $, ApiError, api, apiMessage, button, categoryPath, element, euros, money, options } from "./ui.js";

export function setupPlanner({ notify, checkFunding = () => {} }) {
  const form = document.querySelector("#scenario-form");
  const resultStatus = document.querySelector("#result-status");
  const message = document.querySelector("#result-message");
  const messageTitle = document.querySelector("#message-title");
  const messageDetail = document.querySelector("#message-detail");
  const results = document.querySelector("#projection-results");
  const planCards = document.querySelector("#plan-cards");
  const planTemplate = document.querySelector("#plan-template");
  const legend = document.querySelector("#chart-legend");
  const canvas = document.querySelector("#cash-chart");
  const schedulePlan = document.querySelector("#schedule-plan");
  const scheduleRows = document.querySelector("#schedule-rows");
  const pagination = document.querySelector("#schedule-pagination");
  const pageStatus = document.querySelector("#page-status");
  const previousPage = document.querySelector("#previous-page");
  const nextPage = document.querySelector("#next-page");

  const planNames = { upfront: "Fund upfront", gradual: "Fund gradually" };
  const monthsPerPage = 12;
  const monthLabels = new Intl.DateTimeFormat(undefined, { month: "short", year: "numeric", timeZone: "UTC" });

  let pendingRequest;
  let invalidField;
  let comparison;
  let selectedPlan = 0;
  let schedulePage = 0;
  let activeScenario;
  let comparedInput;
  let initialized = false;
  const saveButton = $("#save-scenario");
  const saveDialog = $("#scenario-dialog");
  const saveForm = $("#scenario-save-form");
  const savedList = $("#saved-scenarios");
  $("#planner-check-funding").addEventListener("click", () => {
    const month = form.elements.dueMonth.value;
    checkFunding({
      amount: form.elements.purchasePrice.value.trim(),
      purchase_on: month ? month + "-01" : undefined,
      label: "Purchase planner",
    });
  });

  function monthLabel(month) {
    return monthLabels.format(new Date(month + "-01T00:00:00Z"));
  }

  function applyInput(input) {
    for (const [field, value] of Object.entries(input)) {
      const control = form.elements.namedItem(field);
      if (control) control.value = value;
    }
  }

  function showMessage(title, detail = "", tone = "") {
    comparison = null;
    comparedInput = null;
    saveButton.disabled = true;
    results.hidden = true;
    resultStatus.textContent = "";
    message.dataset.tone = tone;
    messageTitle.textContent = title;
    messageDetail.textContent = detail;
    messageDetail.hidden = detail === "";
    message.hidden = false;
  }

  function clearIssue() {
    if (invalidField) {
      invalidField.removeAttribute("aria-invalid");
      invalidField.removeAttribute("aria-describedby");
      invalidField = null;
    }
  }

  function showError(error) {
    if (!(error instanceof ApiError)) {
      showMessage("Dashboard connection unavailable", apiMessage(error), "error");
      return;
    }
    showMessage("Check the scenario", error.message, "error");
    invalidField = form.elements.namedItem(error.field ?? "");
    if (invalidField) {
      invalidField.setAttribute("aria-invalid", "true");
      invalidField.setAttribute("aria-describedby", "message-detail");
      invalidField.focus();
    }
  }

  function beginRequest() {
    pendingRequest?.abort();
    pendingRequest = new AbortController();
    clearIssue();
    return pendingRequest;
  }

  async function comparePlans() {
    const request = beginRequest();
    const scenario = Object.fromEntries(new FormData(form));
    showMessage("Comparing funding plans");
    try {
      const result = await api("/api/plans", { method: "POST", data: scenario, signal: request.signal });
      if (!request.signal.aborted) {
        showComparison(result, scenario);
      }
    } catch (error) {
      if (!request.signal.aborted) {
        showError(error);
      }
    }
  }

  async function loadExample() {
    const request = beginRequest();
    showMessage("Loading the car purchase example");
    try {
      const example = await api("/api/example", { signal: request.signal });
      if (request.signal.aborted) return;
      activeScenario = null;
      applyInput(example);
      await comparePlans();
    } catch (error) {
      if (!request.signal.aborted) {
        showError(error);
      }
    }
  }

  function planCard(plan, index) {
    const card = planTemplate.content.firstElementChild.cloneNode(true);
    card.dataset.approach = plan.approach;
    card.querySelector("[data-plan-title]").textContent = planNames[plan.approach];
    card.querySelector("[data-contribution]").textContent = money(plan.projection[0].contribution);
    card.querySelector("[data-lowest]").textContent = money(plan.lowestAvailableCash);
    card.querySelector("[data-closing]").textContent = money(plan.projection.at(-1).availableCash);
    card.querySelector("button").addEventListener("click", () => selectPlan(index));
    return card;
  }

  function legendItem(approach, caption) {
    const item = document.createElement("li");
    item.dataset.approach = approach;
    item.textContent = caption;
    return item;
  }

  function showComparison(result, input) {
    if (!result.plans.length) {
      showMessage("No feasible plan", "Additional starting cash needed is " + money(result.shortfall) + ".", "shortfall");
      comparedInput = input;
      saveButton.disabled = false;
      return;
    }
    comparedInput = input;
    saveButton.disabled = false;
    comparison = result;
    const months = result.plans[0].projection;
    const range = `${monthLabel(months[0].month)} – ${monthLabel(months.at(-1).month)}`;
    const count = result.plans.length === 1 ? "1 feasible plan" : `${result.plans.length} feasible plans`;
    resultStatus.textContent = `${count} · ${range}`;
    planCards.replaceChildren(...result.plans.map(planCard));
    legend.replaceChildren(
      ...result.plans.map(plan => legendItem(plan.approach, planNames[plan.approach])),
      legendItem("reserve", `Reserve ${money(result.reserve)}`),
    );
    message.hidden = true;
    results.hidden = false;
    selectPlan(0);
    paintChart();
  }

  function selectPlan(index) {
    selectedPlan = index;
    schedulePage = 0;
    for (const [cardIndex, card] of Array.from(planCards.children).entries()) {
      const checked = cardIndex === index;
      card.classList.toggle("selected", checked);
      card.querySelector("button").setAttribute("aria-pressed", String(checked));
    }
    renderSchedule();
  }

  function scheduleRow(month) {
    const row = document.createElement("tr");
    const date = document.createElement("th");
    date.scope = "row";
    date.textContent = monthLabel(month.month);
    row.append(date);
    for (const amount of [month.netCashFlow, month.contribution, month.purchase, month.availableCash, month.earmarkedCash]) {
      const cell = document.createElement("td");
      cell.textContent = money(amount);
      row.append(cell);
    }
    return row;
  }

  function renderSchedule() {
    const plan = comparison.plans[selectedPlan];
    const count = plan.projection.length;
    const pages = Math.ceil(count / monthsPerPage);
    schedulePage = Math.min(pages - 1, Math.max(0, schedulePage));
    const first = schedulePage * monthsPerPage;
    const end = Math.min(first + monthsPerPage, count);
    schedulePlan.textContent = planNames[plan.approach];
    scheduleRows.replaceChildren(...plan.projection.slice(first, end).map(scheduleRow));
    pagination.hidden = pages === 1;
    pageStatus.textContent = `Months ${first + 1}–${end} of ${count}`;
    previousPage.disabled = schedulePage === 0;
    nextPage.disabled = schedulePage === pages - 1;
  }

  function peakCash(plans, reserve) {
    let peak = BigInt(reserve) > 100n ? BigInt(reserve) : 100n;
    for (const plan of plans) {
      for (const month of plan.projection) {
        const amount = BigInt(month.availableCash);
        if (amount > peak) {
          peak = amount;
        }
      }
    }
    return peak;
  }

  function paintChart() {
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!comparison || width === 0 || height === 0) {
      return;
    }
    const scale = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const context = canvas.getContext("2d");
    context.setTransform(scale, 0, 0, scale, 0, 0);
    const style = getComputedStyle(canvas);
    context.font = `13px ${style.fontFamily}`;
    context.textBaseline = "middle";

    const { plans, reserve } = comparison;
    const months = plans[0].projection;
    const peak = peakCash(plans, reserve);
    const ticks = Array.from({ length: 5 }, (_, index) => peak * BigInt(index) / 4n);
    const labels = ticks.map(money);
    const labelWidth = Math.max(...labels.map(label => context.measureText(label).width));
    const left = Math.min(Math.ceil(labelWidth) + 12, width * .47);
    const right = width - 15;
    const top = 18;
    const bottom = height - 36;
    const xFor = index => months.length === 1 ? (left + right) / 2 : left + index * (right - left) / (months.length - 1);
    const yFor = amount => bottom - Number(amount) / Number(peak) * (bottom - top);

    const reserveY = yFor(reserve);
    context.fillStyle = "#fdf8f1";
    context.fillRect(left, reserveY, right - left, bottom - reserveY);
    context.textAlign = "right";
    for (const [index, tick] of ticks.entries()) {
      const y = yFor(tick);
      context.strokeStyle = "#dce5ed";
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(left, y);
      context.lineTo(right, y);
      context.stroke();
      context.fillStyle = "#50657a";
      context.fillText(labels[index], left - 8, y, Math.max(1, left - 8));
    }

    context.strokeStyle = style.getPropertyValue("--reserve").trim();
    context.lineWidth = 1.5;
    context.setLineDash([6, 5]);
    context.beginPath();
    context.moveTo(left, reserveY);
    context.lineTo(right, reserveY);
    context.stroke();
    context.setLineDash([]);

    for (const { approach, projection } of plans) {
      const color = style.getPropertyValue(`--${approach}`).trim();
      const last = projection.length - 1;
      const stride = Math.max(1, Math.ceil(last / (right - left)));
      context.strokeStyle = color;
      context.lineWidth = 2.7;
      context.lineJoin = "round";
      context.beginPath();
      context.moveTo(xFor(0), yFor(projection[0].availableCash));
      for (let index = stride; index < last; index += stride) {
        context.lineTo(xFor(index), yFor(projection[index].availableCash));
      }
      context.lineTo(xFor(last), yFor(projection[last].availableCash));
      context.stroke();
      context.fillStyle = color;
      for (const index of new Set([0, last])) {
        context.beginPath();
        context.arc(xFor(index), yFor(projection[index].availableCash), 3.7, 0, Math.PI * 2);
        context.fill();
      }
    }

    context.fillStyle = "#50657a";
    context.textAlign = "center";
    const labelCount = Math.min(months.length, Math.max(2, Math.floor((right - left) / 79) + 1));
    for (let label = 0; label < labelCount; label += 1) {
      const index = labelCount === 1 ? 0 : Math.round(label * (months.length - 1) / (labelCount - 1));
      const text = monthLabel(months[index].month);
      const half = context.measureText(text).width / 2 + 1;
      const x = Math.min(width - half, Math.max(half, xFor(index)));
      context.fillText(text, x, bottom + 18);
    }
  }

  form.addEventListener("submit", event => {
    event.preventDefault();
    comparePlans();
  });
  form.addEventListener("input", () => {
    pendingRequest?.abort();
    clearIssue();
    showMessage("Scenario edited", "Compare plans to update the monthly projection.");
  });
  document.querySelector("#load-example").addEventListener("click", loadExample);
  previousPage.addEventListener("click", () => { schedulePage -= 1; renderSchedule(); });
  nextPage.addEventListener("click", () => { schedulePage += 1; renderSchedule(); });
  new ResizeObserver(paintChart).observe(canvas);

  function nextDueMonth(start) {
    const due = new Date(start + "-01T00:00:00Z");
    due.setUTCMonth(due.getUTCMonth() + 6);
    return due.toISOString().slice(0, 7);
  }

  async function useLedger(clearPurchase = false) {
    const request = beginRequest();
    showMessage("Loading ledger balances");
    try {
      const input = await api("/api/planner-inputs", { signal: request.signal });
      if (request.signal.aborted) return;
      if (clearPurchase) {
        activeScenario = null;
        applyInput({ purchasePrice: "", minimumReserve: "0.00", dueMonth: nextDueMonth(input.startingMonth) });
      }
      if (!form.elements.dueMonth.value || form.elements.dueMonth.value < input.startingMonth) {
        form.elements.dueMonth.value = nextDueMonth(input.startingMonth);
      }
      if (!form.elements.minimumReserve.value) form.elements.minimumReserve.value = "0.00";
      applyInput(input);
      showMessage("Ledger balances loaded", "Review the purchase details and compare plans.");
      if (clearPurchase) form.elements.purchasePrice.focus();
    } catch (error) {
      if (!request.signal.aborted) showError(error);
    }
  }

  async function loadSavedScenario(id) {
    const request = beginRequest();
    showMessage("Loading saved comparison");
    try {
      const saved = await api("/api/scenarios/" + id, { signal: request.signal });
      if (request.signal.aborted) return;
      activeScenario = saved;
      applyInput(saved.input);
      showComparison(saved.result, saved.input);
    } catch (error) {
      if (!request.signal.aborted) showError(error);
    }
  }

  async function refreshSaved() {
    try {
      const items = await api("/api/scenarios");
      if (!items.length) {
        savedList.replaceChildren(element("p", "", "Saved comparisons will appear here."));
        return;
      }
      savedList.replaceChildren(...items.map(item => {
        const row = element("div", "saved-row");
        const open = button(item.name, "text-button");
        const remove = button("", "icon-link", "trash", "Delete " + item.name);
        open.addEventListener("click", () => loadSavedScenario(item.id));
        remove.addEventListener("click", async () => {
          if (!window.confirm("Delete this saved comparison?")) return;
          try {
            await api("/api/scenarios/" + item.id, { method: "DELETE" });
            if (activeScenario?.id === item.id) activeScenario = null;
            notify("Comparison deleted.");
            refreshSaved();
          } catch (error) {
            notify(apiMessage(error));
          }
        });
        const actual = element("a", "text-link", "Plan vs. actual");
        actual.href = "#forecast/actual/" + item.id;
        row.append(open, actual, remove);
        return row;
      }));
    } catch (error) {
      savedList.replaceChildren(element("p", "", apiMessage(error)));
    }
  }

  const saveIssue = $("#scenario-save-error");
  const targetDetails = element("details", "forecast-targets", element("summary", "", "Monthly category estimates"));
  const targetRows = element("div", "forecast-target-rows");
  const addTarget = button("Add category estimate", "secondary");
  const targetHelp = element("p", "muted", "Save the estimates you used to build monthly cash flow. Plan vs. actual can compare each estimate with ledger activity.");
  targetDetails.append(targetHelp, targetRows, addTarget);
  saveIssue.before(targetDetails);
  let targetCategories = [];
  let targetLoad = 0;

  function targetRow(target = {}) {
    const row = element("div", "forecast-target-row");
    const category = element("select");
    category.dataset.targetCategory = "";
    category.required = true;
    const categoryMap = new Map(targetCategories.map(item => [item.id, item]));
    const descriptions = { income: "Income", expense: "Expense", investment: "Investment" };
    options(category, targetCategories.map(item => [item.id,
      descriptions[item.kind] + " · " + categoryPath(item, categoryMap)]), target.category_id, "Choose category");
    const categoryLabel = element("label", "field", "Category");
    categoryLabel.append(category);
    const amount = element("input");
    amount.dataset.targetAmount = "";
    amount.inputMode = "decimal";
    amount.required = true;
    amount.value = target.amount == null ? "" : euros(target.amount);
    const amountLabel = element("label", "field", "Monthly amount in euros");
    amountLabel.append(amount);
    const remove = button("Remove estimate", "text-button");
    remove.addEventListener("click", () => row.remove());
    row.append(categoryLabel, amountLabel, remove);
    targetRows.append(row);
  }

  addTarget.addEventListener("click", () => targetRow());
  saveButton.addEventListener("click", async () => {
    if (!comparedInput) return;
    saveIssue.hidden = true;
    saveForm.elements.name.value = activeScenario?.name ?? "";
    targetRows.replaceChildren();
    targetDetails.open = false;
    addTarget.disabled = true;
    const request = ++targetLoad;
    saveDialog.showModal();
    saveForm.elements.name.focus();
    const estimates = comparedInput.varianceTargets ?? activeScenario?.input?.varianceTargets ?? [];
    const saveControl = $("button[type=submit]", saveForm);
    saveControl.disabled = Boolean(estimates.length);
    try {
      const setup = await api("/api/forecast/setup");
      if (request !== targetLoad || !saveDialog.open) return;
      targetCategories = setup.categories;
      addTarget.disabled = !targetCategories.length;
      targetDetails.open = Boolean(estimates.length);
      targetHelp.textContent = targetCategories.length
        ? "Save the estimates you used to build monthly cash flow. Plan vs. actual can compare each estimate with ledger activity."
        : "Add a category in your ledger to save a monthly estimate for it.";
      for (const estimate of estimates) targetRow(estimate);
      saveControl.disabled = false;
    } catch (error) {
      if (request === targetLoad && saveDialog.open) {
        saveIssue.textContent = apiMessage(error);
        saveIssue.hidden = false;
      }
    }
  });
  $("#cancel-scenario-save").addEventListener("click", () => saveDialog.close());
  saveForm.addEventListener("submit", async event => {
    event.preventDefault();
    const submit = $("button[type=submit]", saveForm);
    if (submit.disabled || !comparedInput) return;
    submit.disabled = true;
    saveIssue.hidden = true;
    const id = activeScenario?.id;
    const name = saveForm.elements.name.value.trim();
    const varianceTargets = [...targetRows.querySelectorAll(".forecast-target-row")].map(row => ({
      category_id: $("[data-target-category]", row).value,
      amount: $("[data-target-amount]", row).value,
    }));
    try {
      const saved = await api("/api/scenarios" + (id ? "/" + id : ""), {
        method: id ? "PUT" : "POST", data: { name, input: comparedInput, varianceTargets },
      });
      activeScenario = { id: saved.id, name };
      saveDialog.close();
      notify("Comparison saved.");
      refreshSaved();
      api("/api/scenarios/" + saved.id).then(record => {
        if (activeScenario?.id === saved.id) activeScenario = record;
      }).catch(() => {});
    } catch (error) {
      saveIssue.textContent = apiMessage(error);
      saveIssue.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });
  $("#new-scenario").addEventListener("click", () => useLedger(true));
  $("#use-ledger").addEventListener("click", () => useLedger());

  return {
    open() {
      if (initialized) {
        paintChart();
        return;
      }
      initialized = true;
      refreshSaved();
      loadExample();
    },
  };
}
