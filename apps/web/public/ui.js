export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

export function element(tag, className = "", content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.append(content);
  return node;
}

export function svg(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

export function icon(name) {
  const node = svg("svg", { class: "icon", "aria-hidden": "true" });
  node.append(svg("use", { href: "/icons.svg#" + name }));
  return node;
}

export function button(label, className = "secondary", iconName = "", accessibleName = "") {
  const node = element("button", className);
  node.type = "button";
  if (iconName) node.append(icon(iconName));
  if (label) node.append(label);
  if (accessibleName) {
    node.setAttribute("aria-label", accessibleName);
    node.title = accessibleName;
  }
  return node;
}

export function empty(iconName, heading, detail, action) {
  const node = element("div", "empty");
  node.append(element("span", "empty-icon", icon(iconName)), element("strong", "", heading));
  if (detail) node.append(element("p", "", detail));
  if (action) node.append(action);
  return node;
}

export class ApiError extends Error {
  constructor(message, field, status) {
    super(message);
    this.field = field;
    this.status = status;
  }
}

export async function api(path, { data, method = "GET", signal, responseType, workspace } = {}) {
  const profile = document.documentElement?.dataset.profileId;
  const scope = workspace ?? (new URLSearchParams(location.search).get("workspace") || profile);
  const headers = data === undefined ? {} : { "Content-Type": "application/json" };
  if (scope) headers["X-Workspace"] = String(scope);
  if (profile) headers["X-Profile"] = profile;
  const response = await fetch(path, {
    method, signal,
    headers,
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  if (response.status === 401) window.dispatchEvent(new Event("sessionexpired"));
  if (response.ok && responseType === "blob") return response.blob();
  const result = await response.json();
  if (!response.ok) throw new ApiError(result.error.message, result.error.field, response.status);
  return result;
}

export async function fileToBase64(file) {
  const url = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(reader.result), { once: true });
    reader.addEventListener("error", () => reject(new ApiError("The file could not be read.", "file")), { once: true });
    reader.readAsDataURL(file);
  });
  return url.slice(url.indexOf(",") + 1);
}

export function apiMessage(error) {
  return error instanceof ApiError ? error.message : "The dashboard could not reach the server. Try again.";
}

const integers = new Intl.NumberFormat();
const fractions = new Intl.NumberFormat(undefined, { minimumIntegerDigits: 2, useGrouping: false });
const decimalPoint = integers.formatToParts(1.5).find(part => part.type === "decimal").value;

export function money(cents, { fixed = false } = {}) {
  if (cents === null || cents === undefined) return "—";
  const amount = BigInt(cents);
  const absolute = amount < 0n ? -amount : amount;
  const fraction = absolute % 100n;
  return (amount < 0n ? "−" : "") + "€" + integers.format(absolute / 100n)
    + (fraction !== 0n || fixed ? decimalPoint + fractions.format(fraction) : "");
}

export function euros(cents) {
  const value = BigInt(cents);
  const amount = value < 0n ? -value : value;
  return (value < 0n ? "-" : "") + String(amount / 100n) + "." + String(amount % 100n).padStart(2, "0");
}

export function dateLabel(iso, options = { day: "numeric", month: "short", year: "numeric" }) {
  return new Intl.DateTimeFormat("en-GB", { ...options, timeZone: "UTC" }).format(new Date(iso + "T00:00:00Z"));
}

export function dateRange(start, end) {
  if (start === end) return dateLabel(end);
  const sameYear = start.slice(0, 4) === end.slice(0, 4);
  return dateLabel(start, sameYear ? { day: "numeric", month: "short" } : undefined) + " – " + dateLabel(end);
}

export const flows = {
  income: { label: "Income", root: "Total income", icon: "income", color: "mint" },
  expense: { label: "Expense", root: "Total expenses", icon: "expense", color: "rose" },
  investment: { label: "Investment", root: "Total investments", icon: "trend", color: "violet" },
};

export const entryTypes = {
  expense: flows.expense,
  income: flows.income,
  investment: flows.investment,
  transfer: { label: "Transfer", icon: "transfer", color: "sky" },
  valuation: { label: "Investment value change", icon: "trend", color: "slate" },
};

export const accountTypes = {
  checking: { label: "Checking", icon: "wallet", color: "indigo" },
  savings: { label: "Savings", icon: "savings", color: "mint" },
  investment: { label: "Investment", icon: "trend", color: "violet" },
  cash: { label: "Cash", icon: "cash", color: "sky" },
};

export const colors = {
  indigo: "Indigo", mint: "Mint", rose: "Rose", sky: "Blue", amber: "Amber", violet: "Violet", slate: "Slate",
};

export function categoryPath(category, categoryMap) {
  const path = [];
  for (let node = category; node; node = categoryMap.get(node.parent_id)) path.unshift(node.name);
  return path.join(" › ");
}

export function options(select, pairs, selected, prompt) {
  if (selected === undefined) selected = select.value;
  const items = prompt === undefined ? pairs : [["", prompt], ...pairs];
  select.replaceChildren(...items.map(([value, label]) => new Option(label, value)));
  if (items.some(([value]) => String(value) === String(selected ?? ""))) select.value = selected ?? "";
}
