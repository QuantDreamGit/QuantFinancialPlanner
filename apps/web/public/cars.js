import { api, apiMessage, button, dateLabel, element, empty, euros, icon, money, options } from "./ui.js";
import { vehicleDiagram } from "./cars-diagram.js";

const endpoint = "/api/cars";
const conditions = { unchecked: "Unchecked", good: "Good", watch: "Monitor", repair: "Needs repair" };
const activityNames = {
  odometer: "Odometer reading", fuel: "Fill-up", repair: "Repair",
  maintenance: "General maintenance", service: "Scheduled service",
};
const tabs = { fuel: "Fuel & mileage", maintenance: "Maintenance", health: "Vehicle health" };
const numbers = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
const km = value => numbers.format(value) + " km";
const paid = value => money(value, { fixed: true });
const optionalEuros = value => value == null ? "" : euros(value);
const ownerNames = car => car.owners.map(person => person.name).join(", ");
const pageSize = 20;

function action(label, callback, className = "secondary", iconName = "", accessibleName = "") {
  const control = button(label, className, iconName, accessibleName);
  control.addEventListener("click", callback);
  return control;
}

function panel(heading, detail, actions) {
  const title = element("div", "", element("h2", "", heading));
  if (detail) title.append(element("p", "", detail));
  const header = element("header", "panel-heading car-panel-heading", title);
  if (actions) header.append(actions);
  return element("section", "panel car-panel", header);
}

function cluster(...controls) {
  const group = element("div", "car-actions");
  group.append(...controls);
  return group;
}

function metric(label, value, caption, symbol) {
  const header = element("div", "metric-heading", element("span", "metric-icon", icon(symbol)));
  header.append(element("h2", "", label));
  const card = element("article", "panel metric");
  card.append(header, element("p", "metric-value", value));
  if (caption) card.append(element("p", "metric-caption", caption));
  return card;
}

function field(container, name, label, { pairs, value, hint, wide, tag, ...properties } = {}) {
  const input = element(tag || (pairs ? "select" : "input"));
  input.name = name;
  input.id = "car-field-" + name;
  Object.assign(input, properties);
  if (pairs) options(input, pairs, value);
  else if (value != null) input.value = value;
  const caption = element("label", "", label);
  caption.htmlFor = input.id;
  const wrapper = element("div", "field" + (wide ? " full" : ""), caption);
  wrapper.append(input);
  if (hint) {
    const help = element("small", "", hint);
    help.id = input.id + "-help";
    input.setAttribute("aria-describedby", help.id);
    wrapper.append(help);
  }
  container.append(wrapper);
  return input;
}

function checkbox(container, name, label, checked) {
  const input = element("input");
  input.type = "checkbox";
  input.name = name;
  input.checked = checked;
  const wrapper = element("label", "car-checkbox", input);
  wrapper.append(element("span", "", label));
  container.append(wrapper);
  return input;
}

function costInput(container, name, label, value, properties = {}) {
  field(container, name, label, { inputMode: "decimal", value: optionalEuros(value), placeholder: "0.00", ...properties });
}

function kmInput(container, name, label, value, properties = {}) {
  field(container, name, label, {
    type: "number", min: 0, max: 10000000, step: 1, required: true, value, ...properties,
  });
}

function notesInput(container, value) {
  field(container, "note", "Notes (optional)", { tag: "textarea", rows: 2, maxLength: 500, value, wide: true });
}

function table(labels, values, totals) {
  const grid = element("table", "car-table");
  const headers = element("tr");
  for (const label of labels) {
    const header = element("th", "", label);
    header.scope = "col";
    headers.append(header);
  }
  const body = element("tbody");
  for (const row of values) {
    const record = element("tr");
    record.append(...row.map(value => element("td", "", value)));
    body.append(record);
  }
  grid.append(element("thead", "", headers), body);
  if (totals) {
    const total = element("tr");
    const label = element("th", "", totals[0]);
    label.scope = "row";
    total.append(label, ...totals.slice(1).map(value => element("td", "", value)));
    grid.append(element("tfoot", "", total));
  }
  return element("div", "table-scroll", grid);
}

function conditionLabel(condition) {
  const label = element("span", "car-condition", conditions[condition]);
  label.dataset.condition = condition;
  return label;
}

function mileageDetail(row) {
  if (!row.note) return row.name || activityNames[row.kind];
  const details = element("div", "car-row-detail", element("strong", "", row.name || activityNames[row.kind]));
  details.append(element("small", "muted", row.note));
  return details;
}

export function setupCars(root, { notify = () => {} } = {}) {
  const title = element("div", "", element("h1", "", "Cars"));
  title.append(element("p", "page-description", "Track running costs, plan mileage-based servicing and inspect your car."));
  const addCar = action("Add car", () => editCar(), "primary", "plus");
  addCar.disabled = true;
  const heading = element("header", "page-heading", title);
  heading.append(addCar);
  const main = element("div", "car-main");
  const errorBox = element("p", "notice");
  errorBox.setAttribute("role", "alert");
  errorBox.hidden = true;

  const dialog = element("dialog", "editor car-dialog");
  dialog.setAttribute("aria-labelledby", "car-dialog-title");
  const dialogTitle = element("h2");
  dialogTitle.id = "car-dialog-title";
  const form = element("form");
  const fields = element("div", "field-grid");
  const formError = element("p", "notice");
  formError.setAttribute("role", "alert");
  formError.hidden = true;
  const cancel = action("Cancel", () => dialog.close());
  const save = element("button", "primary", "Save");
  save.type = "submit";
  const controls = element("div", "dialog-actions", cluster(cancel, save));
  form.append(element("header", "dialog-heading", dialogTitle), fields, formError, controls);
  dialog.append(form);
  root.replaceChildren(heading, errorBox, main, dialog);

  let garage;
  let state;
  let activeId;
  let request;
  let activeTab;
  let activeView = "exterior";
  let activePart;
  let historyLimit = pageSize;
  let commit;
  let busy = false;

  async function open({ list = true, focus } = {}) {
    request?.abort();
    const current = new AbortController();
    request = current;
    const { signal } = current;
    errorBox.hidden = true;
    if (!main.children.length) main.append(element("p", "muted", "Loading cars…"));
    try {
      if (list) {
        const result = await api(endpoint, { signal });
        if (signal.aborted) return;
        garage = result;
      }
      activeTab ??= garage.group_costs ? "group" : "fuel";
      activeId = garage.cars.some(car => car.id === activeId) ? activeId : garage.cars[0]?.id;
      const result = activeId && activeTab !== "group" ? await api(endpoint + "/" + activeId, { signal }) : null;
      if (signal.aborted) return;
      state = result;
      addCar.disabled = false;
      render();
      if (focus) main.querySelector(focus)?.focus();
    } catch (error) {
      if (signal.aborted) return;
      if (!garage) main.replaceChildren();
      errorBox.replaceChildren(element("span", "", apiMessage(error)), action("Retry", () => open()));
      errorBox.hidden = false;
    }
  }

  function openForm(label, submitLabel, build, submit, danger = false) {
    dialogTitle.textContent = label;
    fields.replaceChildren();
    formError.hidden = true;
    save.textContent = submitLabel;
    save.className = danger ? "danger-button" : "primary";
    build(fields);
    commit = submit;
    if (!dialog.open) dialog.showModal();
    (fields.querySelector("input, select, textarea") || cancel).focus();
  }

  async function mutation(path, method, values, { list = false, message, focus, choose } = {}) {
    const result = await api(endpoint + path, { method, data: values });
    if (choose) activeId = result.id;
    dialog.close();
    if (message) notify(typeof message === "function" ? message(result) : message);
    await open({ list: list || Boolean(garage.group_costs), focus });
  }

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !commit) return;
    const data = new FormData(form);
    busy = cancel.disabled = save.disabled = true;
    formError.hidden = true;
    for (const input of fields.querySelectorAll("[aria-invalid]")) input.removeAttribute("aria-invalid");
    try {
      await commit(Object.fromEntries(data), data);
    } catch (error) {
      formError.textContent = apiMessage(error);
      formError.hidden = false;
      const input = form.elements.namedItem(error.field);
      if (input instanceof HTMLElement) {
        input.setAttribute("aria-invalid", "true");
        input.focus();
      }
    } finally {
      busy = cancel.disabled = save.disabled = false;
    }
  });
  dialog.addEventListener("cancel", event => { if (busy) event.preventDefault(); });

  function editCar(car) {
    openForm(car ? "Edit car" : "Add car", car ? "Save car" : "Add car", formFields => {
      field(formFields, "name", "Car name", {
        value: car?.name, maxLength: 80, required: true, placeholder: "e.g. Fiat Panda", wide: true,
      });
      field(formFields, "drivetrain", "Drivetrain", {
        pairs: Object.entries(garage.drivetrains), value: car?.drivetrain || "fwd", wide: true,
      });
      const equipment = element("fieldset", "car-equipment full", element("legend", "", "Optional components"));
      for (const [key, label] of Object.entries(garage.equipment)) {
        checkbox(equipment, key, label, car?.equipment.includes(key) ?? false);
      }
      formFields.append(equipment);
      if (!car || car.can_manage) addAssignmentFields(formFields, car);
      if (car?.can_manage) {
        formFields.append(action("Delete car", () => remove(
          car.name, "/" + car.id, "This permanently deletes the car, its activity, schedules and inspections.",
          { list: true, focus: "#car-picker" },
        ), "secondary car-destructive full", "trash"));
      }
    }, (data, raw) => {
      const equipment = Object.keys(garage.equipment).filter(key => raw.has(key));
      const values = { name: data.name, drivetrain: data.drivetrain, equipment };
      const selected = new Set(raw.getAll("workspace_ids").map(Number));
      if (!car || (car.can_manage && (selected.size !== car.assignments.length ||
          car.assignments.some(item => !selected.has(item.id))))) values.workspace_ids = [...selected];
      return mutation(car ? "/" + car.id : "", car ? "PUT" : "POST", values,
        { list: true, choose: true, message: "Car saved.", focus: "#car-picker" });
    });
  }

  function addAssignmentFields(formFields, car) {
    const sharing = element("fieldset", "car-equipment full", element("legend", "", "Assign this car"));
    sharing.append(element("p", "muted car-sharing-help", "Choose the profiles and group workspaces where this car appears."));
    const selected = new Set(car ? car.assignments.map(item => item.id) : [garage.workspace_id]);
    const choices = new Map(garage.assignment_options.map(item => [item.id, item]));
    for (const item of car?.assignments ?? []) choices.set(item.id, item);
    const columns = element("div", "car-assignment-types");
    for (const [kind, label] of [["profile", "People"], ["group", "Groups"]]) {
      const items = [...choices.values()].filter(item => item.kind === kind);
      if (!items.length) continue;
      const column = element("div", "", element("h3", "", label));
      for (const item of items) {
        checkbox(column, "workspace_ids", item.name, selected.has(item.id)).value = String(item.id);
      }
      columns.append(column);
    }
    sharing.append(columns);
    formFields.append(sharing);
  }

  function remove(label, path, detail, config = {}) {
    openForm("Delete " + label + "?", "Delete", formFields => {
      formFields.append(element("p", "muted full", detail));
    }, () => mutation(path, "DELETE", undefined, { message: "Deleted.", ...config }), true);
  }

  function editActivity(kind, row = null, schedule = null) {
    const label = activityNames[kind];
    const path = "/" + state.car.id + "/activities" + (row ? "/" + row.id : "");
    openForm(row ? "Edit " + label.toLowerCase() : "Log " + label.toLowerCase(), "Save entry", formFields => {
      if (schedule) formFields.append(element("p", "muted full", schedule.name));
      if (["repair", "maintenance"].includes(kind) || (row && kind === "service" && !row.schedule_id)) {
        field(formFields, "name", "Work performed", {
          value: row?.name, maxLength: 80, required: true, placeholder: "e.g. Replace front brake pads", wide: true,
        });
      }
      field(formFields, "recorded_on", "Date", {
        type: "date", required: true, max: state.today, value: row?.recorded_on ?? state.today,
      });
      kmInput(formFields, "odometer_km", "Total odometer (km)", row?.odometer_km ?? state.odometer?.odometer_km);
      if (kind === "fuel") {
        field(formFields, "litres", "Litres added", {
          inputMode: "decimal", required: true, placeholder: "0.000",
          value: row ? row.fuel_ml / 1000 : "",
        });
        costInput(formFields, "cost", "Total paid (€)", row?.cost, { required: true });
        const tank = element("div", "car-tank full");
        checkbox(tank, "full_tank", "Filled the tank completely", row?.full_tank ?? false);
        tank.append(element("small", "muted", "Two full tanks let us measure consumption. Record partial fills in between."));
        formFields.append(tank);
      } else if (kind !== "odometer") {
        costInput(formFields, "cost", "Total paid (€)", row?.cost);
      }
      notesInput(formFields, row?.note);
      if (row) {
        const detail = row.schedule_id
          ? "Deleting this entry recalculates the next service from the remaining history."
          : "This entry will be removed from the car’s history.";
        formFields.append(action("Delete entry", () => remove("this entry", path, detail),
          "secondary car-destructive full", "trash"));
      }
    }, (data, raw) => mutation(path, row ? "PUT" : "POST", {
      ...data, kind, schedule_id: schedule?.id, full_tank: raw.has("full_tank"),
    }, { message: "Entry saved." }));
  }

  function importMileage() {
    const path = "/" + state.car.id + "/odometer-import";
    openForm("Import odometer readings", "Import readings", formFields => {
      const instructions = element("div", "car-import-guide full", element("p", "",
        "Use a UTF-8 CSV with a header, dates in YYYY-MM-DD format, and whole kilometres. Up to 1,000 rows."));
      instructions.append(element("pre", "", "date,odometer_km\n" + state.today + ",85000"));
      formFields.append(instructions);
      field(formFields, "csv", "CSV file", { type: "file", accept: ".csv,text/csv", required: true, wide: true });
    }, async data => {
      if (data.csv.size > 120_000) {
        formError.textContent = "Choose a CSV file smaller than 120 KB.";
        formError.hidden = false;
        return;
      }
      await mutation(path, "POST", { csv: await data.csv.text() }, {
        message: result => result.imported + " readings imported" +
          (result.duplicates ? " · " + result.duplicates + " already recorded." : "."),
      });
    });
  }

  function editSchedule(row) {
    const path = "/" + state.car.id + "/schedules" + (row ? "/" + row.id : "");
    openForm(row ? "Edit service interval" : "Add service interval", "Save interval", formFields => {
      field(formFields, "name", "Service name", {
        value: row?.name, required: true, maxLength: 80, placeholder: "e.g. Oil change", wide: true,
      });
      kmInput(formFields, "interval_km", "Repeat every (km)", row?.interval_km, { min: 1, placeholder: "20000" });
      kmInput(formFields, "baseline_km", "Starting odometer (km)", row?.baseline_km ?? state.odometer?.odometer_km, {
        hint: "Mileage at the last service before logging here, or when this interval started.",
      });
      costInput(formFields, "estimate", "Estimated cost (€)", row?.estimate, {
        hint: "Leave blank to use the last recorded cost for this service.", wide: true,
      });
      if (row) formFields.append(action("Delete interval", () => remove(row.name, path,
        "This removes the service interval. Completed services stay in the cost history."), "secondary car-destructive full", "trash"));
    }, data => mutation(path, row ? "PUT" : "POST", data, { message: "Service interval saved." }));
  }

  function editPart(part) {
    const path = "/" + state.car.id + "/parts/" + part.id;
    openForm(part.name, "Save inspection", formFields => {
      const status = element("fieldset", "car-equipment full", element("legend", "", "Condition"));
      const choices = element("div", "car-condition-choices");
      for (const value of Object.keys(conditions)) {
        const radio = element("input");
        radio.name = "condition";
        radio.type = "radio";
        radio.value = value;
        radio.checked = part.condition === value;
        const wrapper = element("label", "car-radio", radio);
        wrapper.append(conditionLabel(value));
        choices.append(wrapper);
      }
      status.append(choices);
      formFields.append(status);
      costInput(formFields, "estimate", "Potential repair cost (€)", part.estimate, {
        wide: true, hint: "Monitor and Needs repair estimates count toward potential repair costs.",
      });
      notesInput(formFields, part.note);
    }, data => mutation(path, "PUT", data, { message: "Inspection saved.", focus: "[data-part=" + part.id + "]" }));
  }

  function changeTab(key) {
    if (key === activeTab) return;
    activeTab = key;
    historyLimit = pageSize;
    const focus = "[data-car-tab=" + key + "]";
    if (key !== "group" && state?.car.id !== activeId) {
      open({ list: false, focus });
    } else {
      render();
      main.querySelector(focus).focus();
    }
  }

  function selectCar(id) {
    activeId = id;
    activeView = "exterior";
    activePart = undefined;
    historyLimit = pageSize;
    open({ list: false, focus: "#car-picker" });
  }

  function render() {
    const group = Boolean(garage.group_costs);
    if (!garage.cars.length) {
      main.replaceChildren(element("section", "panel car-empty", empty("car", group ? "No cars assigned to this group" : "Add your first car",
        group ? "Add a group car, or use Edit car on an existing car to share it here."
          : "Record the odometer to start planning fuel and maintenance costs.",
        action("Add car", () => editCar(), "primary", "plus"))));
      return;
    }
    const navigation = element("div", "car-tabs");
    navigation.setAttribute("role", "group");
    navigation.setAttribute("aria-label", "Car sections");
    const sections = group ? { group: "Group costs", ...tabs } : tabs;
    for (const [key, label] of Object.entries(sections)) {
      const tab = action(label, () => changeTab(key), "");
      tab.dataset.carTab = key;
      tab.setAttribute("aria-pressed", String(key === activeTab));
      navigation.append(tab);
    }
    const content = element("div", "car-content");
    if (activeTab === "group") {
      renderGroupCosts(content);
      main.replaceChildren(navigation, content);
      return;
    }
    const car = state.car;
    const profile = element("div", "panel car-profile");
    const summary = element("div", "car-profile-name", element("span", "car-profile-icon", icon("car")));
    const profileCopy = element("div", "", element("h2", "", car.name));
    profileCopy.append(element("p", "muted", garage.drivetrains[car.drivetrain]));
    profileCopy.append(element("p", "muted", car.owners.length ? "Assigned to " + ownerNames(car) : "Group car"));
    summary.append(profileCopy);
    const picker = element("select");
    picker.id = "car-picker";
    picker.setAttribute("aria-label", "Switch car");
    options(picker, garage.cars.map(item => [item.id, item.name + " · " + (ownerNames(item) || "Group car")]), car.id);
    picker.addEventListener("change", () => selectCar(Number(picker.value)));
    profile.append(summary, cluster(picker, action("Edit car", () => editCar(car), "secondary", "edit")));
    if (activeTab === "fuel") renderFuel(content);
    if (activeTab === "maintenance") renderMaintenance(content);
    if (activeTab === "health") renderHealth(content);
    if (group) main.replaceChildren(navigation, profile, content);
    else main.replaceChildren(profile, navigation, content);
  }

  function renderGroupCosts(host) {
    const { cars, actual, forecast, coverage, upcoming } = garage.group_costs;
    const byId = new Map(garage.cars.map(car => [car.id, car]));
    const actualAmounts = amounts => [paid(amounts.fuel), paid(amounts.maintenance), paid(amounts.repair), paid(amounts.total)];
    const forecastAmounts = amounts => [paid(amounts.fuel), paid(amounts.scheduled), paid(amounts.planned), paid(amounts.health_estimates)];
    const recorded = panel("Recorded car costs", "All recorded payments. Maintenance includes general work and completed scheduled services.");
    recorded.append(table(["Car", "Fuel", "Maintenance", "Repairs", "Total"], cars.map(row => {
      const car = byId.get(row.car_id);
      const detail = element("div", "car-row-detail", action(car.name, () => {
        activeTab = "fuel";
        selectCar(car.id);
      }, "text-button car-cost-name"));
      detail.append(element("small", "muted", ownerNames(car) || "Group car"));
      return [detail, ...actualAmounts(row.actual)];
    }), ["Group total", ...actualAmounts(actual)]));

    const outlook = panel("12-month cost forecast", "Uses each car’s driving and fuel history, service intervals and repair estimates from Vehicle health.");
    outlook.append(table(["Car", "Fuel", "Scheduled maintenance", "Fuel + maintenance", "Potential repairs"],
      cars.map(row => [byId.get(row.car_id).name, ...forecastAmounts(row.forecast)]),
      ["Known group forecast", ...forecastAmounts(forecast)]));
    const notes = [];
    if (coverage.missing_fuel) notes.push(coverage.missing_fuel === 1 ? "1 car needs more fuel or mileage history"
      : coverage.missing_fuel + " cars need more fuel or mileage history");
    if (coverage.undated_schedules) notes.push(coverage.undated_schedules === 1 ? "1 service interval needs more mileage data"
      : coverage.undated_schedules + " service intervals need more mileage data");
    if (coverage.unpriced_services) notes.push(coverage.unpriced_services === 1 ? "1 service visit needs a cost estimate"
      : coverage.unpriced_services + " service visits need cost estimates");
    if (coverage.unpriced_parts) notes.push(coverage.unpriced_parts === 1 ? "1 flagged part needs a repair estimate"
      : coverage.unpriced_parts + " flagged parts need repair estimates");
    if (notes.length) outlook.append(element("p", "muted car-cost-coverage", notes.join(" · ")));

    const services = panel("Upcoming services", "Next service due per interval, with the projected number of visits in the next 12 months.");
    if (upcoming.length) services.append(table(["Car", "Service", "Next due", "Visits", "Cost per visit"], upcoming.map(row => {
      const timing = element("div", "car-row-detail", element("strong", "", km(row.due_km)));
      if (row.remaining_km !== null && row.remaining_km <= 0) {
        timing.append(element("small", "car-overdue", row.remaining_km < 0 ? km(-row.remaining_km) + " overdue" : "Due now"));
      } else {
        timing.append(element("small", "muted", row.due_on ? "Estimated " + dateLabel(row.due_on) : "Date needs more mileage data"));
      }
      return [byId.get(row.car_id).name, row.name, timing, row.count_in_year ?? "—", paid(row.projected_cost)];
    })));
    else services.append(element("p", "muted car-empty-row", "Services due in this period will appear here."));
    host.append(recorded, outlook, services);
  }

  function renderFuel(host) {
    const { odometer, driving, fuel, projection } = state;
    const metrics = element("div", "car-metrics");
    metrics.append(
      metric("Latest odometer", odometer ? km(odometer.odometer_km) : "—",
        odometer ? dateLabel(odometer.recorded_on) : "Log or import a reading", "car"),
      metric("Fuel consumption", fuel.consumption == null ? "—" : numbers.format(fuel.consumption) + " L/100 km",
        fuel.measured_km ? "Measured over " + km(fuel.measured_km) : "Requires two full fill-ups", "bar"),
      metric("Fuel spent", paid(fuel.cost),
        numbers.format(fuel.litres) + " L recorded" +
        (fuel.price_per_litre == null ? "" : " · " + paid(fuel.price_per_litre) + "/L"), "wallet"),
    );
    const outlook = panel("Driving & fuel outlook", driving
      ? "Driving pace from " + dateLabel(driving.from) + " to " + dateLabel(driving.through) + "."
      : "Add readings on two different dates with distance travelled to estimate future driving.");
    const forecast = element("dl", "car-stat-grid");
    const items = [
      ["Projected distance in 12 months", driving ? km(driving.km_in_year) : "—"],
      ["Fuel cost per km", fuel.cost_per_km == null ? "—" : paid(fuel.cost_per_km) + "/km"],
      ["Projected fuel cost in 12 months", paid(projection.fuel)],
    ];
    for (const [label, value] of items) {
      const item = element("div", "", element("dt", "", label));
      item.append(element("dd", "", value));
      forecast.append(item);
    }
    outlook.append(forecast);
    const actions = cluster(
      action("Log odometer", () => editActivity("odometer")),
      action("Import CSV", importMileage),
      action("Log fill-up", () => editActivity("fuel"), "primary", "plus"),
    );
    const history = panel("Fuel & odometer history", "", actions);
    renderHistory(history, ["fuel", "odometer"]);
    host.append(metrics, outlook, history);
  }

  function renderMaintenance(host) {
    const { projection, schedules, paid: totals } = state;
    const estimates = element("div", "car-metrics");
    const serviceCaption = [];
    if (projection.unpriced_services) serviceCaption.push(projection.unpriced_services + " due without a cost");
    if (projection.undated_schedules) serviceCaption.push(projection.undated_schedules + " without a mileage forecast");
    estimates.append(
      metric("Maintenance & repairs paid", paid(BigInt(totals.repair) + BigInt(totals.maintenance) + BigInt(totals.service)),
        "All recorded service and repair costs", "wallet"),
      metric("Scheduled service · next 12 months", paid(projection.scheduled),
        serviceCaption.join(" · ") || (schedules.length ? "Based on your mileage and service intervals" : "Add a service interval"), "calendar"),
      metric("Potential repairs", paid(projection.health_estimates),
        projection.unpriced_parts ? projection.unpriced_parts + " flagged without an estimate" : "From flagged parts in Vehicle health", "car"),
    );
    const servicePanel = panel("Scheduled maintenance", "Log completed services to reset their kilometre intervals.",
      action("Add interval", () => editSchedule(), "primary", "plus"));
    if (!schedules.length) {
      servicePanel.append(element("p", "car-empty-row muted", "Set up a recurring service, such as an oil change every 20,000 km."));
    } else {
      servicePanel.append(table(["Service", "Next due", "Estimated cost", "Actions"], schedules.map(row => {
        const description = element("div", "car-row-detail", element("strong", "", row.name));
        description.append(element("small", "muted", "Every " + km(row.interval_km)),
          element("small", "muted", "Last service at " + km(row.last_service_km) +
            (row.last_service_on ? " · " + dateLabel(row.last_service_on) : "")));
        const next = element("div", "car-row-detail", element("strong", "", km(row.due_km)));
        if (row.remaining_km == null) next.append(element("small", "muted", "Log an odometer reading"));
        else if (row.remaining_km < 0) next.append(element("small", "car-overdue", km(-row.remaining_km) + " overdue"));
        else if (row.remaining_km === 0) next.append(element("small", "car-overdue", "Due now"));
        else {
          next.append(element("small", "muted", km(row.remaining_km) + " from last reading"));
          if (row.due_on) next.append(element("small", "muted", "Estimated " + dateLabel(row.due_on)));
        }
        return [description, next, paid(row.projected_cost), cluster(
          action("Log service", () => editActivity("service", null, row), "secondary", "", "Log " + row.name),
          action("", () => editSchedule(row), "icon-link", "edit", "Edit " + row.name),
        )];
      })));
    }
    const history = panel("Maintenance & repair history", "", cluster(
      action("Log repair", () => editActivity("repair")),
      action("Log general maintenance", () => editActivity("maintenance"), "primary", "plus"),
    ));
    renderHistory(history, ["repair", "maintenance", "service"]);
    host.append(estimates, servicePanel, history);
  }

  function renderHistory(host, kinds) {
    const entries = state.activity.filter(row => kinds.includes(row.kind));
    if (!entries.length) {
      host.append(element("p", "car-empty-row muted", "Your recorded entries will appear here."));
      return;
    }
    const showFuel = kinds.includes("fuel");
    const columns = ["Date", "Entry", "Odometer", showFuel ? "Fuel" : "Type", "Paid", "Actions"];
    const records = entries.slice(0, historyLimit).map(row => {
      const schedule = state.schedules.find(scheduled => scheduled.id === row.schedule_id);
      return [
        dateLabel(row.recorded_on), mileageDetail(row), km(row.odometer_km),
        showFuel ? row.fuel_ml ? numbers.format(row.fuel_ml / 1000) + " L · " + (row.full_tank ? "Full" : "Partial") : "—"
          : activityNames[row.kind],
        paid(row.cost),
        action("", () => editActivity(row.kind, row, schedule), "icon-link", "edit",
          "Edit " + (row.name || activityNames[row.kind]).toLowerCase() + " from " + dateLabel(row.recorded_on)),
      ];
    });
    host.append(table(columns, records));
    if (entries.length > historyLimit) host.append(action("Show older entries", () => {
      historyLimit += pageSize;
      render();
      const focus = main.querySelector("[data-load-history]") || main.querySelector(".car-table tbody tr:last-child button");
      focus?.focus();
    }, "secondary car-history-more"));
    const more = host.querySelector(".car-history-more");
    if (more) more.dataset.loadHistory = "";
  }

  function renderHealth(host) {
    const { parts, projection, car } = state;
    const counts = { unchecked: 0, good: 0, watch: 0, repair: 0 };
    for (const part of parts) counts[part.condition]++;
    const health = element("div", "car-health");
    const diagram = panel("Vehicle diagram", "Choose a view and select a part to check its condition.");
    const viewpoints = element("div", "segmented car-views");
    viewpoints.setAttribute("role", "group");
    viewpoints.setAttribute("aria-label", "Vehicle views");
    for (const [key, label] of Object.entries(garage.views)) {
      const view = action(label, () => {
        if (activeView === key) return;
        activeView = key;
        activePart = undefined;
        render();
        main.querySelector("[data-car-view=" + key + "]").focus();
      }, "");
      view.dataset.carView = key;
      view.setAttribute("aria-pressed", String(key === activeView));
      viewpoints.append(view);
    }
    const inView = parts.filter(part => part.view === activeView);
    const selected = inView.find(part => part.id === activePart) || inView[0];
    activePart = selected.id;
    const selectPart = (key, marker) => {
      activePart = key;
      render();
      main.querySelector(marker ? "[data-part=" + key + "]" : "#car-part-picker")?.focus();
    };
    diagram.append(viewpoints, vehicleDiagram(car, activeView, inView, selected.id, conditions, selectPart));
    const sidebar = element("div", "car-health-sidebar");
    const overview = panel("Condition summary", (parts.length - counts.unchecked) + " of " + parts.length + " parts checked");
    const summary = element("dl", "car-condition-summary");
    for (const [condition, count] of Object.entries(counts)) {
      const row = element("div", "", element("dt", "", conditionLabel(condition)));
      row.append(element("dd", "", numbers.format(count)));
      summary.append(row);
    }
    const estimated = element("div", "car-health-total", element("strong", "", "Potential repair costs"));
    estimated.append(element("strong", "", paid(projection.health_estimates)));
    overview.append(summary, estimated);
    if (projection.unpriced_parts) overview.append(element("p", "muted",
      projection.unpriced_parts + (projection.unpriced_parts === 1 ? " flagged part needs" : " flagged parts need") + " an estimate."));

    const inspection = panel("Part inspection", "");
    const control = field(inspection, "part", "Selected part", {
      id: "car-part-picker", pairs: inView.map(part => [part.id, part.name]), value: selected.id,
    });
    control.addEventListener("change", () => selectPart(control.value, false));
    const detail = element("div", "car-part-detail", conditionLabel(selected.condition));
    if (selected.estimate !== null) detail.append(element("strong", "", paid(selected.estimate) + " estimated"));
    if (selected.note) detail.append(element("p", "muted", selected.note));
    inspection.append(detail, action("Edit inspection", () => editPart(selected), "primary", "edit"));
    sidebar.append(inspection, overview);
    health.append(diagram, sidebar);
    host.append(health);
  }

  return { open };
}
