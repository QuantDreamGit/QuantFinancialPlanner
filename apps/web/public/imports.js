import { api, apiMessage, button, categoryPath, dateLabel, element, euros, fileToBase64, money, options } from "./ui.js";

const endpoint = "/api/imports/";
const sourceNames = { csv: "Bank or card CSV", pdf: "PDF statements", amazon: "Amazon order export" };
const pdfChoices = [["local", "Local extraction"], ["ai", "AI with Groq"], ["openai", "AI with OpenAI"]];
const pdfProviders = {
  ai: { name: "Groq", settings: "ai", keyEndpoint: "groq-key", keysUrl: "https://console.groq.com/keys",
    consent: "Send the locally extracted text from each selected PDF to Groq to identify transactions." },
  openai: { name: "OpenAI", settings: "openai", keyEndpoint: "openai-key", keysUrl: "https://platform.openai.com/api-keys",
    consent: "Send each selected PDF in full to OpenAI, including scanned pages, to identify transactions." },
};
const fieldNames = {
  date: "Posting date", name: "Description", amount: "Signed amount", debit: "Debit (money out)",
  credit: "Credit (money in)", direction: "Debit/credit indicator", currency: "Currency", reference: "Bank transaction ID",
};
const modeChoices = [
  ["debit-negative", "Negative amounts are debits"], ["debit-positive", "Positive amounts are debits"],
  ["separate", "Separate debit and credit columns"],
];
const dateChoices = [
  ["auto", "ISO or unambiguous dates"], ["dmy", "Day / month / year"],
  ["mdy", "Month / day / year"], ["ymd", "Year / month / day"],
];
const pageSize = 25;
const commitSize = 4000;
const smallExpenseLimit = 25000n;
const otherMovementKinds = new Set(["transfer", "investment", "valuation", "settlement"]);
const otherMovementActions = new Set(["transfer", "settle", "expected"]);
const archiveEndpoint = endpoint + "documents";
const fileSize = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const reviewGroups = {
  ready: ["Ready", "New transactions with clear details and verified matches."],
  matches: ["Proposed matches", "Compare the statement and ledger before applying a proposed match."],
  review: ["Needs review", "Check repeated rows, extracted values and rows with unresolved details."],
  handled: ["Already handled", "These rows have already been recorded or reconciled."],
};

function select(pairs, value) {
  const node = element("select");
  options(node, pairs, value);
  return node;
}

function label(text, control) {
  const wrapper = element("label", "field");
  wrapper.append(element("span", "", text), control);
  return wrapper;
}

function action(text, callback, className = "secondary", icon = "") {
  const node = button(text, className, icon);
  node.addEventListener("click", callback);
  return node;
}

function panel(title) {
  const section = element("section", "panel imports-panel");
  section.append(element("h2", "", title));
  return section;
}

function textInput(value, name, type = "text") {
  const node = element("input");
  node.type = type;
  node.value = value ?? "";
  node.setAttribute("aria-label", name);
  return node;
}

async function encodeFile(file) {
  if (!file) throw new Error("Choose a file to read.");
  if (file.size > 12 * 1024 * 1024) throw new Error("Choose a file up to 12 MB.");
  return fileToBase64(file);
}

function download(content, name, mime) {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const anchor = element("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function countLabel(count, singular, plural = singular + "s") {
  return count + " " + (count === 1 ? singular : plural);
}

function archiveSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  const unit = bytes >= 1024 ** 2 ? "MB" : "KB";
  return fileSize.format(bytes / (unit === "MB" ? 1024 ** 2 : 1024)) + " " + unit;
}

function csvCell(value, text = false) {
  let safe = String(value ?? "");
  if (text && /^[\s]*[=+\-@]/.test(safe)) safe = "'" + safe;
  return '"' + safe.replaceAll('"', '""') + '"';
}

function normalizedCsv(rows) {
  return "Date,Description,Amount,Currency,Reference\r\n" + rows.map(row =>
    [csvCell(row.posted_on), csvCell(row.name, true), csvCell(row.value, !/^-?\d+(?:[.,]\d{1,2})?$/.test(row.value)),
      csvCell(reviewedCurrency(row), true), csvCell(row.reference, true)].join(",")
  ).join("\r\n") + "\r\n";
}

function reviewedCurrency(row) {
  return row.currency_unconfirmed && row.currency_confirmed ? "EUR" : row.currency;
}

function rowExport(row) {
  return {
    date: row.posted_on || null, description: row.name, amount: row.value || null,
    currency: reviewedCurrency(row) || null, reference: row.reference || null,
    statement_description: row.statement_description || row.original.name || null,
    source: row.origins?.[0] ?? null,
    ...(row.origins?.length > 1 ? { additional_sources: row.origins.slice(1) } : {}),
  };
}

function parsedCents(value) {
  const match = /^([+−-]?)(\d+)(?:[.,](\d{1,2}))?$/.exec(value.trim());
  if (!match) return null;
  const magnitude = BigInt(match[2]) * 100n + BigInt((match[3] ?? "").padEnd(2, "0"));
  return match[1] === "-" || match[1] === "−" ? -magnitude : magnitude;
}

function pdfSpending(rows) {
  const smallByMonth = new Map();
  const large = [], credits = [], other = [], attention = [];
  let expenses = 0n, creditTotal = 0n, otherNet = 0n;
  for (const row of rows) {
    const cents = parsedCents(row.value);
    const currency = reviewedCurrency(row);
    const issues = row.errors.filter(issue => !(row.currency_confirmed && issue === "Confirm that this row is in euros."));
    if (row.review_hold) issues.push("The extracted totals do not agree with the original statement.");
    if (row.overlap || issues.length || !cents || !/^\d{4}-\d{2}-\d{2}$/.test(row.posted_on)
      || (currency !== "EUR" && currency !== "€")) {
      if (row.overlap) issues.push(row.overlap);
      if (!issues.length) issues.push("Check the date, amount and currency against the original.");
      attention.push({ row, issues });
      continue;
    }
    const kind = row.action === "link" ? row.matches.find(match => String(match.id) === row.entry_id)?.kind
      : row.action === "commitment" ? row.commitments.find(item => item.id + "|" + item.date === row.commitment)?.kind
        : row.kind;
    if (otherMovementActions.has(row.action) || otherMovementKinds.has(kind)) {
      otherNet += cents;
      other.push({ row, cents });
      continue;
    }
    if (cents > 0n) {
      creditTotal += cents;
      credits.push({ row, cents });
      continue;
    }
    const spend = -cents;
    expenses += spend;
    if (spend > smallExpenseLimit) {
      large.push({ row, cents: spend });
      continue;
    }
    const month = row.posted_on.slice(0, 7);
    const entry = smallByMonth.get(month) ?? { month, cents: 0n, count: 0 };
    entry.cents += spend;
    entry.count++;
    smallByMonth.set(month, entry);
  }
  const descendingDate = (a, b) => b.row.posted_on.localeCompare(a.row.posted_on);
  return {
    expenses, creditTotal, otherNet, large: large.sort(descendingDate), credits: credits.sort(descendingDate),
    other: other.sort(descendingDate), attention,
    months: [...smallByMonth.values()].sort((a, b) => b.month.localeCompare(a.month)),
  };
}

function normalizedPdfJson(result) {
  const review = pdfSpending(result.rows);
  return JSON.stringify({
    name: result.name,
    extraction_method: result.pdf_method,
    expense_threshold: euros(smallExpenseLimit),
    statements: result.files.map(file => ({
      file: file.filename, archive_id: file.document?.id ?? null, notices: file.notices,
      extraction: file.extraction ?? null, error: file.error ?? null,
    })),
    total_expenses: euros(review.expenses),
    small_expenses_by_month: review.months.map(month => ({ month: month.month, total: euros(month.cents), count: month.count })),
    large_expenses: review.large.map(item => rowExport(item.row)),
    total_credits: euros(review.creditTotal),
    credits: review.credits.map(item => rowExport(item.row)),
    other_movement_net: euros(review.otherNet),
    other_movements: review.other.map(item => rowExport(item.row)),
    needs_attention: review.attention.map(item => ({ ...rowExport(item.row), issues: item.issues })),
    rows: result.rows.map(rowExport),
  }, null, 2) + "\n";
}

function mergePdfRows(rows) {
  const repeated = new Map();
  const removed = new Set();
  for (const row of rows) {
    if (!row.review_key) continue;
    const group = repeated.get(row.review_key) ?? [];
    group.push(row);
    repeated.set(row.review_key, group);
  }
  for (const group of repeated.values()) {
    const files = new Set(group.map(row => row.fileIndex));
    if (files.size < 2) continue;
    const first = group[0];
    const sameReference = first.reliable_reference && files.size === group.length
      && group.every(row => row.reliable_reference && !row.errors.length && !row.review_hold
        && row.reference === first.reference && row.value === first.value
        && row.posted_on === first.posted_on && row.currency === first.currency);
    if (sameReference) {
      const chosen = group.find(row => row.existing) ?? group.find(row => row.review_group === "ready") ?? first;
      for (const row of group) {
        if (row === chosen) continue;
        chosen.origins.push(...row.origins);
        removed.add(row);
      }
      continue;
    }
    for (const row of group) {
      row.overlap = "A similar row appears in another selected statement. Check for overlap before applying.";
      if (!row.existing) row.review_group = "review";
    }
  }
  const merged = rows.filter(row => !removed.has(row));
  const linked = new Map();
  for (const row of merged) {
    if (row.auto_match == null || row.existing) continue;
    const matches = linked.get(row.auto_match) ?? [];
    matches.push(row);
    linked.set(row.auto_match, matches);
  }
  for (const matches of linked.values()) {
    if (matches.length < 2) continue;
    for (const row of matches) {
      row.matchOverlap = "Another statement row matches this ledger transaction. Compare the original statements.";
      if (!row.overlap) row.review_group = "matches";
    }
  }
  return merged;
}

function previewLabel(result) {
  const title = result.name || sourceNames[result.source] || "Statement";
  const provider = pdfProviders[result.pdf_method];
  return title + (provider ? " · Extracted with " + provider.name : "");
}

function sourceLabel(row) {
  const first = row.origins?.[0];
  if (!first) return "";
  const page = first.page ? " · Page " + first.page : "";
  const other = row.origins.length > 1 ? " · +" + (row.origins.length - 1) + " more" : "";
  return first.file + page + other;
}

function summaryTable(title, headings, rows) {
  const table = element("table");
  table.setAttribute("aria-label", title);
  const header = element("tr");
  header.append(...headings.map(name => element("th", "", name)));
  const body = element("tbody");
  for (const values of rows) {
    const line = element("tr");
    line.append(...values.map(value => element("td", "", value)));
    body.append(line);
  }
  table.append(element("thead", "", header), body);
  const scroll = element("div", "table-scroll imports-summary-table", table);
  scroll.tabIndex = 0;
  scroll.setAttribute("role", "region");
  scroll.setAttribute("aria-label", title);
  return scroll;
}

function rowSummary(item) {
  const { row, cents } = item;
  return [dateLabel(row.posted_on), row.statement_description || row.original.name || row.name,
    money(cents, { fixed: true }), sourceLabel(row)];
}

function renderPdfSpending(container, rows) {
  const headings = ["Date", "Statement description", "Amount", "Source"];
  const review = pdfSpending(rows);
  const breakdown = element("div", "imports-summary-grid");
  const monthly = element("section", "imports-summary-group");
  const large = element("section", "imports-summary-group");
  monthly.append(element("h3", "", "Expenses of €250 or less by month"));
  large.append(element("h3", "", "Each expense over €250"));
  monthly.append(review.months.length ? summaryTable("Monthly small expenses", ["Month", "Transactions", "Total"],
    review.months.map(month => [dateLabel(month.month + "-01", { month: "long", year: "numeric" }),
      String(month.count), money(month.cents, { fixed: true })])) : element("p", "muted", "No expenses of €250 or less."));
  large.append(review.large.length ? summaryTable("Expenses over €250", headings, review.large.map(rowSummary))
    : element("p", "muted", "No expenses over €250."));
  breakdown.append(monthly, large);
  const title = element("div", "imports-spending-total");
  title.append(element("h3", "", "Total expenses"), element("strong", "", money(review.expenses, { fixed: true })));
  const credits = element("details", "imports-summary-detail");
  credits.append(element("summary", "", "Credits · " + money(review.creditTotal, { fixed: true })));
  credits.append(review.credits.length ? summaryTable("Credits", headings, review.credits.map(rowSummary))
    : element("p", "muted", "No credits."));
  container.replaceChildren(title, breakdown, credits);
  if (review.other.length) {
    const other = element("details", "imports-summary-detail");
    other.append(element("summary", "", "Transfers and settlements · " + money(review.otherNet, { fixed: true })),
      summaryTable("Transfers and settlements", headings, review.other.map(rowSummary)));
    container.append(other);
  }
  if (review.attention.length) {
    const attention = element("details", "imports-summary-detail");
    attention.append(element("summary", "", "Needs attention · " + countLabel(review.attention.length, "row")));
    attention.append(summaryTable("Rows needing attention", ["Date", "Statement description", "Source", "Issue"],
      review.attention.map(({ row, issues }) => [row.posted_on ? dateLabel(row.posted_on) : "—",
        row.statement_description || row.original.name || row.name || "—", sourceLabel(row), issues.join(" ")])));
    container.append(attention);
  }
}

export function setupImports(root, { notify = () => {}, onChange = () => {} } = {}) {
  const heading = element("div", "page-heading");
  const title = element("div");
  const mainTitle = element("h1", "", "Import payments");
  mainTitle.id = "imports-heading";
  title.append(mainTitle, element("p", "page-description", "Review statements and Amazon orders against your ledger before recording anything."));
  heading.append(title, action("PDF archive", () => {
    archiveBox.scrollIntoView({ block: "start" });
    archiveBox.focus({ preventScroll: true });
  }, "secondary", "paperclip"));
  const status = element("p", "notice");
  status.hidden = true;
  status.setAttribute("role", "status");
  const settings = panel("Choose statements or an export");
  const grid = element("div", "field-grid imports-settings");
  const source = select(Object.entries(sourceNames), "csv");
  const account = select([], "");
  const file = element("input");
  file.type = "file";
  const fileCaption = element("span");
  const fileLabel = element("label", "field");
  fileLabel.append(fileCaption, file);
  const importName = textInput("", "Import name");
  importName.maxLength = 100;
  const pdfMethod = select(pdfChoices, "local");
  const pdfMethodLabel = label("PDF extraction", pdfMethod);
  const aiPanel = element("div", "imports-ai");
  aiPanel.hidden = true;
  const consent = element("input");
  consent.type = "checkbox";
  const consentLabel = element("label", "imports-check imports-consent");
  const consentCopy = element("span");
  consentLabel.append(consent, consentCopy);
  const consentDetails = element("p", "muted", "Statements may contain financial and identifying details.");
  const apiKey = textInput("", "API key", "password");
  apiKey.autocomplete = "off";
  apiKey.autocapitalize = "off";
  apiKey.spellcheck = false;
  const keyLabel = label("API key", apiKey);
  const keyLink = element("a", "text-link");
  keyLink.target = "_blank";
  keyLink.rel = "noreferrer noopener";
  const keyPanel = element("div", "imports-ai-key");
  const keyState = element("div", "imports-ai-key-state");
  const keyStatus = element("p", "muted");
  keyStatus.setAttribute("role", "status");
  const keyControls = element("div", "imports-ai-key-actions");
  const keyChange = action("Change key", changeKey);
  const keyRemove = action("Remove key", () => storeKey("DELETE"), "text-button");
  const keySave = action("Save key", () => storeKey("PUT"));
  const keyCancel = action("Cancel", cancelKey, "text-button");
  const keyHint = element("p", "muted imports-ai-key-hint");
  apiKey.setAttribute("aria-describedby", "imports-key-hint");
  keyHint.id = "imports-key-hint";
  keyControls.append(keyChange, keyRemove);
  keyState.append(keyStatus, keyControls);
  keyPanel.append(keyLabel, keySave, keyCancel, keyLink, keyHint);
  aiPanel.append(keyState, keyPanel, consentLabel, consentDetails);
  const ocr = element("input");
  ocr.type = "checkbox";
  const ocrLabel = element("label", "imports-check");
  ocrLabel.append(ocr, "Read scanned pages with local OCR");
  const delimiters = select([["auto", "Detect from header"], [",", "Comma"], [";", "Semicolon"], ["tab", "Tab"], ["|", "Pipe"]], "auto");
  const dates = select(dateChoices, "auto");
  const amountMode = select(modeChoices, "debit-negative");
  const modeLabel = label("Amount convention", amountMode);
  const dateLabelNode = label("Date convention", dates);
  const delimiterLabel = label("CSV separator", delimiters);
  const info = element("p", "muted imports-explanation");
  const mappingPanel = element("div", "imports-mapping");
  const settingsActions = element("div", "form-actions");
  const read = action("Read columns", inspect, "secondary");
  const review = action("Review transactions", preview, "primary");
  settingsActions.append(read, review);
  grid.append(label("Source", source), label("Ledger account", account), fileLabel,
    label("Import name (optional)", importName), pdfMethodLabel, delimiterLabel, dateLabelNode, modeLabel);
  settings.append(grid, info, ocrLabel, aiPanel, mappingPanel, settingsActions);
  const resultBox = element("div");
  const archiveBox = panel("Original PDF statements");
  archiveBox.tabIndex = -1;
  const archiveContent = element("div", "imports-archive");
  const archiveStatus = element("p", "muted imports-download-status");
  archiveStatus.setAttribute("role", "status");
  archiveBox.append(archiveContent, archiveStatus);
  const historyBox = panel("Recent imports");
  const history = element("div", "imports-history");
  historyBox.append(history);
  root.replaceChildren(heading, status, settings, resultBox, archiveBox, historyBox);

  let setup, headers, mapping = {}, sourceFile, encoded, result, previewName, busy = false, page = 0, group = "ready";
  let archiveRequest;
  let editingKey = false;
  const controls = [source, account, file, importName, pdfMethod, consent, apiKey, delimiters, dates, amountMode, ocr];
  function setStatus(text = "") {
    status.textContent = text;
    status.hidden = !text;
  }

  function showError(error) {
    setStatus(error.name === "Error" ? error.message : apiMessage(error));
  }

  function pdfProvider() {
    return source.value === "pdf" ? pdfProviders[pdfMethod.value] : undefined;
  }

  function defaultName() {
    return setup && source.value === "pdf" ? "Bill - " + setup.today : "";
  }

  function effectiveName() {
    return importName.value.trim() || defaultName();
  }

  function resetConsent() {
    consent.checked = false;
  }

  function resetAiDraft() {
    resetConsent();
    editingKey = false;
    apiKey.value = "";
  }

  function changeKey() {
    editingKey = true;
    updateAvailability();
    apiKey.focus();
  }

  function cancelKey() {
    editingKey = false;
    apiKey.value = "";
    updateAvailability();
    if (keyChange.hidden) apiKey.focus();
    else keyChange.focus();
  }

  async function storeKey(method) {
    const provider = pdfProvider();
    const ai = setup?.[provider?.settings];
    if (busy || !ai || (method === "PUT" && !ai.can_save_key)) return;
    const key = method === "PUT" ? apiKey.value.trim() : "";
    if ((method === "PUT" && !key) || (method === "DELETE" && !ai.saved_key)) return;
    apiKey.value = "";
    setStatus();
    setBusy(true);
    let changed = false;
    try {
      const response = await api(endpoint + provider.keyEndpoint, {
        method, data: method === "PUT" ? { api_key: key } : undefined,
      });
      setup[provider.settings] = response.ai;
      editingKey = false;
      changed = true;
      notify(provider.name + (method === "PUT" ? " key saved for your account." : " key removed."));
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
      if (changed) {
        if (!keyChange.hidden) keyChange.focus();
        else if (!keyPanel.hidden) apiKey.focus();
      }
    }
  }

  function keyAvailability() {
    const provider = pdfProvider();
    if (!provider) return false;
    const ai = setup?.[provider.settings];
    const draft = apiKey.value.trim();
    const savedUnavailable = Boolean(ai?.saved_key && !ai.configured);
    const requiresDraft = !ai?.configured || (editingKey && !ai.saved_key);
    keyPanel.hidden = !ai || savedUnavailable || (!requiresDraft && !editingKey);
    keyChange.hidden = !ai?.can_save_key || !ai.configured || editingKey;
    keyChange.textContent = ai?.saved_key ? "Change key" : "Use my own key";
    keyChange.disabled = busy;
    keyRemove.hidden = !ai?.saved_key || editingKey;
    keyRemove.disabled = busy;
    keySave.hidden = !ai?.can_save_key;
    keySave.disabled = busy || !draft;
    keyCancel.hidden = !editingKey && !draft;
    keyCancel.textContent = editingKey ? "Cancel" : "Clear";
    keyCancel.disabled = busy;
    const keyName = provider.name + " key";
    const copy = !ai ? "Checking " + keyName + " availability…"
      : savedUnavailable ? "Your saved " + keyName + " is unavailable. Remove it to choose another key."
        : ai.saved_key ? "Using the " + keyName + " saved for your account."
          : ai.server_key || ai.configured ? "Using the server’s " + keyName + "." : "Enter your " + keyName + " to use AI extraction.";
    if (keyStatus.textContent !== copy) keyStatus.textContent = copy;
    keyHint.textContent = ai?.saved_key
      ? "Save the new key before starting an extraction."
      : ai?.can_save_key ? "Save the key now. A successful first extraction with this key also saves it."
        : "This key is used for the selected statements.";
    return Boolean(savedUnavailable || (editingKey && ai?.saved_key) || (requiresDraft && !draft));
  }

  function updateAvailability() {
    read.hidden = source.value !== "csv";
    const provider = pdfProvider();
    const missingPdf = source.value === "pdf" && pdfMethod.value !== "openai" && setup && !setup.pdf_enabled;
    const keyUnavailable = keyAvailability();
    const needsConsent = Boolean(provider && (!consent.checked || keyUnavailable));
    const fileCount = file.files.length;
    read.disabled = busy || !fileCount;
    review.disabled = busy || !setup || !fileCount || !account.value || !!missingPdf
      || needsConsent || (source.value === "csv" && !headers);
    review.textContent = busy ? "Processing…" : provider ? "Extract and review with " + provider.name : "Review transactions";
  }

  function syncSource(initial = false) {
    const isCsv = source.value === "csv";
    const isPdf = source.value === "pdf";
    const provider = pdfProvider();
    const directPdf = isPdf && pdfMethod.value === "openai";
    modeLabel.hidden = dateLabelNode.hidden = source.value === "amazon" || !!provider;
    delimiterLabel.hidden = !isCsv;
    ocrLabel.hidden = !isPdf || directPdf || !setup?.ocr_enabled;
    pdfMethodLabel.hidden = !isPdf;
    aiPanel.hidden = !provider;
    if (provider) {
      consentCopy.textContent = provider.consent;
      const name = provider.name + " API key";
      keyLabel.firstElementChild.textContent = name;
      apiKey.setAttribute("aria-label", name);
      keyLink.textContent = "Get a key from " + provider.name;
      keyLink.href = provider.keysUrl;
    }
    importName.placeholder = defaultName() || "Optional";
    if (!isCsv && amountMode.value === "separate") amountMode.value = "debit-negative";
    amountMode.options[2].disabled = !isCsv;
    file.multiple = isPdf;
    fileCaption.textContent = isPdf ? "PDF statements for this account" : "Statement or export";
    file.accept = { csv: ".csv,.tsv,.txt", pdf: ".pdf,application/pdf", amazon: ".csv,.zip,.json" }[source.value];
    info.replaceChildren();
    if (source.value === "amazon") {
      const link = element("a", "text-link", "Amazon Request My Data");
      link.href = "https://www.amazon.com/hz/privacy-central/data-requests/preview.html";
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      info.append("Request your order history through ", link,
        " in your regional Amazon account, then select its retail order-history CSV or ZIP. Physical orders JSON from Amazon Data Portability is supported.");
    } else if (source.value === "pdf") {
      info.textContent = directPdf
        ? "OpenAI reads the PDF directly, including scanned statements. You can select several statements for this ledger account."
        : setup && !setup.pdf_enabled
          ? "Local PDF extraction needs Poppler or PDFKit. You can also choose OpenAI or use a CSV statement."
          : provider
          ? "The server first extracts text from the PDF. Enable local OCR for scanned pages."
        : "Use a text-based PDF. Check every extracted date and amount against the statement. For a scan, run local OCR or ask your bank for a CSV.";
      if (directPdf || setup?.pdf_enabled) info.append(" Each original PDF is archived after extraction.");
    } else {
      info.textContent = "Choose the account the statement belongs to. Map its date, description, and signed amount or debit and credit columns.";
    }
    if (!initial) discardPreview(true);
    updateAvailability();
  }

  function discardPreview(discardMapping = false) {
    result = null;
    previewName = null;
    resultBox.replaceChildren();
    setStatus();
    if (discardMapping) {
      headers = null;
      mapping = {};
      mappingPanel.replaceChildren();
    }
  }

  function setBusy(flag) {
    busy = flag;
    for (const control of controls) control.disabled = flag;
    updateAvailability();
    for (const control of mappingPanel.querySelectorAll("select")) control.disabled = flag;
  }

  function originalAction(item, feedback, text = "Download original PDF") {
    const control = action(text, async () => {
      control.disabled = true;
      feedback.textContent = "";
      try {
        const blob = await api(archiveEndpoint + "/" + encodeURIComponent(item.id), { responseType: "blob" });
        download(blob, item.filename || "Statement.pdf", "application/pdf");
      } catch (error) {
        feedback.textContent = apiMessage(error);
      } finally {
        control.disabled = false;
      }
    });
    if (item.filename) control.setAttribute("aria-label", "Download original PDF " + item.filename);
    return control;
  }

  async function loadArchive(requestedPage = 1, focusPage = false) {
    archiveRequest?.abort();
    const request = new AbortController();
    archiveRequest = request;
    archiveContent.setAttribute("aria-busy", "true");
    archiveStatus.textContent = "";
    try {
      const archive = await api(archiveEndpoint + "?page=" + requestedPage, { signal: request.signal });
      if (request.signal.aborted) return;
      if (!archive.documents.length) {
        archiveContent.replaceChildren(element("p", "muted", "Original PDF statements appear here after extraction."));
        return;
      }
      const list = element("ul", "imports-archive-list");
      const accountNames = new Map((setup?.accounts ?? []).map(item => [item.id, item.name]));
      for (const item of archive.documents) {
        const info = element("div", "imports-archive-file");
        info.append(element("strong", "", item.name || item.filename));
        const details = [accountNames.get(item.account_id), item.name && item.filename !== item.name && item.filename,
          dateLabel(item.archived_on.slice(0, 10)), archiveSize(item.size)].filter(Boolean);
        info.append(element("small", "muted", details.join(" · ")));
        const row = element("li", "imports-archive-row");
        row.append(info, originalAction(item, archiveStatus));
        list.append(row);
      }
      archiveContent.replaceChildren(list);
      const pages = Math.ceil(archive.total / archive.size);
      if (pages > 1) {
        const pagination = element("div", "pagination");
        const current = element("span", "", "Page " + archive.page + " of " + pages);
        current.tabIndex = -1;
        const previous = action("Previous", () => loadArchive(archive.page - 1, true));
        previous.disabled = archive.page <= 1;
        const next = action("Next", () => loadArchive(archive.page + 1, true));
        next.disabled = archive.page >= pages;
        pagination.append(previous, current, next);
        archiveContent.append(pagination);
        if (focusPage) current.focus();
      }
    } catch (error) {
      if (!request.signal.aborted) archiveStatus.textContent = apiMessage(error);
    } finally {
      if (!request.signal.aborted) archiveContent.setAttribute("aria-busy", "false");
    }
  }

  function historyItem(item) {
    const details = element("details", "imports-history-row");
    const summary = element("summary");
    const sourceName = sourceNames[item.source] ?? item.source;
    summary.append(element("strong", "", item.name || sourceName), document.createTextNode(" · "
      + (item.name ? sourceName + " · " : "") + (item.account ?? "Removed account") + " · " + item.count + " row" + (item.count === 1 ? "" : "s")
      + " · " + dateLabel(item.created_at.slice(0, 10))));
    const content = element("div", "imports-history-details");
    const savedReview = element("div", "imports-history-report");
    const table = element("table");
    const headings = element("tr");
    const titles = ["Date", "Description", "Signed amount", "Applied action", "Reference"];
    if (item.source === "pdf") titles.push("Source");
    headings.append(...titles.map(text => element("th", "", text)));
    table.append(element("thead", "", headings));
    const body = element("tbody");
    table.append(body);
    const tableBox = element("div", "table-scroll", table);
    const actions = { create: "Recorded", link: "Linked", transfer: "Transfer", commitment: "Commitment", expected: "Scheduled settlement", settle: "Shared settlement" };
    let loaded = 0;
    const errorBox = element("p", "muted");
    const more = action("Load more rows", load);
    async function load() {
      more.disabled = true;
      try {
        const batch = await api(endpoint + "batch/" + item.id + "?page=" + (Math.floor(loaded / 40) + 1));
        const documents = new Map((batch.documents ?? []).map(document => [document.id, document]));
        if (!loaded && item.source === "pdf" && batch.report) {
          savedReview.append(element("strong", "", "Expenses in this import · " + money(batch.report.expense_total, { fixed: true })),
            element("p", "muted", "Credits · " + money(batch.report.credits_total, { fixed: true })));
          if (BigInt(batch.report.other_net)) {
            savedReview.append(element("p", "muted", "Transfers and settlements · " + money(batch.report.other_net, { fixed: true })));
          }
          const actions = element("div", "imports-toolbar");
          for (const document of documents.values()) actions.append(originalAction(document, errorBox, "Download " + document.filename));
          savedReview.append(actions);
          if (batch.report.small_groups.length) {
            savedReview.append(summaryTable("Recorded small expenses by month", ["Month", "Transactions", "Total"],
              batch.report.small_groups.map(month => [dateLabel(month.month + "-01", { month: "long", year: "numeric" }),
                String(month.count), money(month.amount, { fixed: true })])));
          }
          if (batch.report.large_expenses.length) {
            savedReview.append(summaryTable("Recorded expenses over €250", ["Date", "Description", "Amount"],
              batch.report.large_expenses.map(row => [dateLabel(row.posted_on), row.statement_description || row.name,
                money(row.amount, { fixed: true })])));
          }
        }
        for (const row of batch.rows) {
          const line = element("tr");
          const values = [row.posted_on ? dateLabel(row.posted_on) : "—",
            row.statement_description || row.name || ("Entry " + row.entry_id),
            money(row.amount, { fixed: true }), actions[row.action] ?? row.action, row.reference || "—"
          ];
          if (item.source === "pdf") {
            const filename = documents.get(row.document_id)?.filename;
            values.push(filename ? filename + (row.page ? " · Page " + row.page : "") : "—");
          }
          line.append(...values.map(value => element("td", "", value)));
          body.append(line);
        }
        loaded += batch.rows.length;
        errorBox.textContent = "";
        tableBox.hidden = !loaded;
        more.hidden = loaded >= batch.total;
      } catch (error) {
        errorBox.textContent = apiMessage(error);
      } finally {
        more.disabled = false;
      }
    }
    tableBox.hidden = true;
    details.addEventListener("toggle", () => { if (details.open && !loaded && !more.disabled) load(); });
    content.append(savedReview, tableBox, errorBox, more);
    details.append(summary, content);
    return details;
  }

  async function open() {
    try {
      setup = await api(endpoint + "setup");
      options(account, setup.accounts.filter(item => item.kind !== "investment").map(item => [item.id, item.name]),
        account.value || setup.accounts.find(item => item.kind !== "investment")?.id,
        "Choose an account");
      history.replaceChildren(...(setup.history.length ? setup.history.map(historyItem)
        : [element("p", "muted", "Reviewed imports will be listed here.")]));
      syncSource(true);
    } catch (error) {
      showError(error);
    }
    await loadArchive();
  }

  async function fileData(selected = file.files[0]) {
    const format = { source: source.value, delimiter: delimiters.value, ocr: !ocrLabel.hidden && ocr.checked };
    if (source.value === "pdf") return { ...format, file: await encodeFile(selected), filename: selected.name };
    if (sourceFile !== selected || !encoded) {
      sourceFile = selected;
      encoded = await encodeFile(sourceFile);
    }
    return { ...format, file: encoded };
  }

  function renderMapping(samples = []) {
    mappingPanel.replaceChildren();
    if (!headers) return;
    const details = element("div", "field-grid");
    const separate = amountMode.value === "separate";
    for (const key of ["date", "name", ...(separate ? ["debit", "credit"] : ["amount", "direction"]), "currency", "reference"]) {
      const control = select([["", "Choose column"], ...headers.map((header, index) => [index, header])], mapping[key] ?? "");
      control.addEventListener("change", () => {
        if (control.value === "") delete mapping[key];
        else mapping[key] = Number(control.value);
        discardPreview();
      });
      const optional = ["direction", "currency", "reference"].includes(key) ? " (optional)" : "";
      details.append(label(fieldNames[key] + optional, control));
    }
    mappingPanel.append(element("h3", "", "Map columns"), details);
    if (samples.length) {
      const content = element("details", "imports-sample");
      const table = element("table");
      const head = element("thead");
      const headRow = element("tr");
      headRow.append(...headers.map(header => element("th", "", header)));
      head.append(headRow);
      const body = element("tbody");
      for (const data of samples) {
        const row = element("tr");
        row.append(...headers.map((_, index) => element("td", "", data[index] ?? "")));
        body.append(row);
      }
      table.append(head, body);
      content.append(element("summary", "", "Inspect first source rows"), element("div", "table-scroll", table));
      mappingPanel.append(content);
    }
  }

  let lastSamples = [];
  async function inspect() {
    if (busy) return;
    setStatus();
    setBusy(true);
    try {
      const details = await api(endpoint + "inspect", { method: "POST", data: await fileData() });
      headers = details.headers;
      mapping = details.mapping;
      lastSamples = details.sample;
      if (mapping.amount === undefined && mapping.debit !== undefined && mapping.credit !== undefined) {
        amountMode.value = "separate";
      }
      renderMapping(lastSamples);
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  function normalize(row, fileResult, fileIndex) {
    const candidates = row.matches.filter(candidate => !candidate.reconciled);
    const match = candidates.find(candidate => candidate.id === row.auto_match) ?? candidates[0];
    const payment = row.expected.length;
    const commitment = row.commitments.length;
    const value = row.amount === null ? "" : euros(row.amount);
    return {
      ...row,
      value,
      original: { name: row.name, posted_on: row.posted_on, value },
      ...(fileResult ? { fileIndex, origins: [{
        file: fileResult.filename, archive_id: fileResult.document?.id ?? null,
        page: row.page ?? null, evidence: row.evidence ?? null, line: row.line, description: row.statement_description || row.name,
      }] } : {}),
      selected: false,
      currency_confirmed: false,
      action: match ? "link" : payment ? "expected" : commitment ? "commitment" : "create",
      entry_id: match ? String(match.id) : "",
      expected_id: payment ? String(row.expected[0].id) : "",
      commitment: commitment ? row.commitments[0].id + "|" + row.commitments[0].date : "",
      other_account_id: "", person_id: "",
      category_id: row.suggested_category_id != null ? String(row.suggested_category_id) : "",
    };
  }

  async function preview() {
    if (review.disabled) return;
    const provider = pdfProvider();
    const key = provider && !setup[provider.settings]?.saved_key ? apiKey.value.trim() : "";
    const approved = consent.checked;
    if (provider) {
      resetConsent();
      apiKey.value = "";
    }
    const name = effectiveName();
    setStatus();
    setBusy(true);
    try {
      const input = { name, account_id: account.value };
      if (!provider) Object.assign(input, { date_format: dates.value, amount_mode: amountMode.value });
      if (source.value === "pdf") {
        input.pdf_method = pdfMethod.value;
        if (provider) input.ai_consent = approved;
        await previewPdfs([...file.files], input, provider, key);
      } else {
        if (source.value === "csv") input.mapping = Object.fromEntries(Object.entries(mapping).filter(([key]) =>
          amountMode.value === "separate" ? !["amount", "direction"].includes(key) : !["debit", "credit"].includes(key)));
        const response = await api(endpoint + "preview", { method: "POST", data: { ...await fileData(), ...input } });
        result = { ...response, name: response.name || name, rows: response.rows.map(row => normalize(row)) };
      }
      editingKey = false;
      page = 0;
      group = "ready";
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
      renderPreview();
    }
  }

  async function previewPdfs(files, input, provider, draftKey) {
    const outcomes = [];
    const rows = [];
    const documents = new Set();
    for (const [index, selected] of files.entries()) {
      const outcome = { filename: selected.name, notices: [] };
      outcomes.push(outcome);
      setStatus("Extracting " + (index + 1) + " of " + files.length + " · " + selected.name);
      try {
        const key = provider && !setup[provider.settings]?.saved_key ? draftKey : "";
        const response = await api(endpoint + "preview", { method: "POST", data: {
          ...await fileData(selected), ...input, ...(key ? { api_key: key } : {}),
        } });
        if (provider && response.ai) setup[provider.settings] = response.ai;
        Object.assign(outcome, { document: response.document, extracted: response.extracted, extraction: response.extraction,
          notices: response.notices ?? [], count: response.rows.length });
        if (response.document && documents.has(response.document.id)) {
          outcome.duplicate = true;
        } else {
          if (response.document) documents.add(response.document.id);
          rows.push(...response.rows.map(row => normalize(row, outcome, index)));
        }
      } catch (error) {
        outcome.error = error.name === "Error" ? error.message : apiMessage(error);
      }
    }
    result = { source: "pdf", name: input.name, pdf_method: input.pdf_method, notices: [], files: outcomes, rows: mergePdfRows(rows) };
    setStatus();
    if (documents.size || input.pdf_method === "openai") void loadArchive();
  }

  function actionFields(row, container, updateRow) {
    container.replaceChildren();
    function addChoices(heading, key, pairs) {
      const control = select([["", "Choose " + heading.toLowerCase()], ...pairs], row[key]);
      control.addEventListener("change", () => { row[key] = control.value; updateRow(); });
      container.append(label(heading, control));
    }
    if (row.action === "link") {
      addChoices("Existing transaction", "entry_id", row.matches.filter(item => !item.reconciled)
        .map(item => [item.id, item.name + " · " + dateLabel(item.posted_on) + " · " + item.kind]));
    } else if (row.action === "transfer") {
      addChoices("Other account", "other_account_id", setup.accounts.filter(item => String(item.id) !== account.value)
        .map(item => [item.id, item.name]));
    } else if (row.action === "settle") {
      addChoices("Person", "person_id", setup.people.map(person => [person.id, person.name + " (@" + person.tag + ")"]));
    } else if (row.action === "commitment") {
      addChoices("Scheduled occurrence", "commitment", row.commitments.map(item => [item.id + "|" + item.date,
        item.name + " · " + dateLabel(item.date)]));
    } else if (row.action === "expected") {
      addChoices("Scheduled settlement", "expected_id", row.expected.map(item => [item.id, "@" + item.tag + " · " + dateLabel(item.date)]));
    } else if (row.action === "create") {
      const kind = /^\s*[−-]/.test(row.value) ? "expense" : "income";
      const map = new Map(setup.categories.map(item => [item.id, item]));
      addChoices("Category", "category_id", setup.categories.filter(item => item.kind === kind)
        .map(item => [item.id, categoryPath(item, map)]));
    }
  }

  function isBulkReady(row) {
    if (row.existing || row.blocked || row.errors.length || row.currency_unconfirmed || row.review_hold
      || row.overlap || row.matchOverlap) return false;
    if (!Object.entries(row.original).every(([field, value]) => row[field] === value)) return false;
    if (row.auto_match != null) return row.action === "link" && row.entry_id === String(row.auto_match);
    return row.review_group === "ready" && row.action === "create";
  }

  function canSelectPdfRow(row) {
    return row.existing === null && !row.blocked && !row.errors.length && !row.review_hold && !row.overlap
      && !row.matchOverlap && row.action === "create"
      && !row.matches.length && !row.commitments.length && !row.expected.length && !isBulkReady(row);
  }

  function compareMatch(row, container) {
    const ledgerMatch = row.matches.find(item => String(item.id) === row.entry_id);
    const commitmentMatch = row.commitments.find(item => item.id + "|" + item.date === row.commitment);
    const settlementMatch = row.expected.find(item => String(item.id) === row.expected_id);
    const choices = {
      link: ledgerMatch && ["Existing transaction", ledgerMatch.name, ledgerMatch.posted_on, ledgerMatch],
      commitment: commitmentMatch && ["Scheduled commitment", commitmentMatch.name, commitmentMatch.date, commitmentMatch],
      expected: settlementMatch && ["Scheduled settlement", "@" + settlementMatch.tag, settlementMatch.date, settlementMatch],
    };
    const reconciled = row.matches.find(item => item.reconciled);
    const proposed = choices[row.action] ?? choices.link ?? choices.commitment ?? choices.expected
      ?? (reconciled && ["Already linked transaction", reconciled.name, reconciled.posted_on, reconciled]);
    container.replaceChildren();
    container.hidden = !proposed;
    if (!proposed) return;
    const [caption, name, day, match] = proposed;
    const card = (heading, description, date, amount) => {
      const details = element("div", "imports-match-side");
      details.append(element("p", "imports-match-caption", heading), element("strong", "", description || "—"),
        element("p", "muted", date ? dateLabel(date) : "Date missing"),
        element("p", "imports-match-amount", amount));
      return details;
    };
    const cents = parsedCents(row.value);
    const statement = card(result.source === "amazon" ? "Amazon order" : "Statement row", row.name, row.posted_on,
      cents === null ? "Check amount" : "Cash movement " + money(cents, { fixed: true }));
    const cash = BigInt(match.amount);
    const isSettlement = match.shared?.type === "settlement" || caption === "Scheduled settlement";
    let matchedAmount = "Cash movement " + money(cash, { fixed: true });
    if (match.shared?.type === "payment") matchedAmount = "Cash paid " + money(match.shared.gross, { fixed: true });
    if (isSettlement) matchedAmount = (cash > 0n ? "Cash received " : "Cash repaid ")
      + money(cash > 0n ? cash : -cash, { fixed: true });
    const existing = card(caption, name, day, matchedAmount);
    if (match.shared?.type === "payment") {
      const owed = BigInt(match.shared_delta);
      existing.append(element("p", "muted", "Your share " + money(match.personal_amount, { fixed: true })
        + (owed ? " · " + (owed > 0 ? "Owed back " : "You owe ") + money(owed > 0 ? owed : -owed, { fixed: true }) : "")));
    }
    if (cents !== null && cents !== cash) {
      existing.append(element("p", "muted", "Cash movements differ."));
    }
    container.append(statement, existing);
  }

  function rowNode(row, updateCount, updateSpending) {
    const section = element("article", "imports-row");
    const head = element("div", "imports-row-heading");
    const pick = element("input");
    pick.type = "checkbox";
    pick.checked = row.selected;
    const readOnly = !!row.existing || row.blocked || busy;
    const rowSource = row.origins?.[0];
    const rowLabelText = "row " + row.line + (rowSource ? " in " + rowSource.file : "");
    pick.setAttribute("aria-label", "Select " + rowLabelText);
    const rowLabel = element("label", "imports-check");
    rowLabel.append(pick, element("strong", "", "Row " + row.line));
    const tag = element("span", "muted");
    const comparison = element("div", "imports-match");
    const updateRow = () => {
      const confirmCurrency = row.currency_unconfirmed && !row.currency_confirmed;
      pick.disabled = readOnly || confirmCurrency;
      let label = row.appliedLabel || "";
      if (row.blocked) label = "Currency unsupported";
      else if (confirmCurrency && !row.existing) label = "Confirm the currency before selecting";
      else if (!row.existing && (row.review_group === "ready" || row.auto_match != null)) {
        if (!isBulkReady(row)) label = "Changed · apply individually";
        else if (row.action === "link") label = "Verified match";
      }
      tag.textContent = label;
      compareMatch(row, comparison);
      updateCount();
    };
    head.append(rowLabel, tag);
    pick.addEventListener("change", () => { row.selected = pick.checked; updateCount(); });
    const values = element("div", "imports-row-values");
    const date = textInput(row.posted_on, "Posting date for row " + row.line, "date");
    date.max = setup.today;
    const name = textInput(row.name, "Description for row " + row.line);
    name.maxLength = 100;
    const amount = textInput(row.value, "Signed amount for row " + row.line);
    amount.inputMode = "decimal";
    amount.maxLength = 28;
    const rowActions = [["create", result.source === "amazon" ? "Record confirmed charge" : "Record transaction"]];
    if (row.matches.some(item => !item.reconciled)) rowActions.push(["link", "Link existing transaction"]);
    if (result.source !== "amazon") {
      if (row.commitments.length) rowActions.push(["commitment", "Match a scheduled commitment"]);
      if (row.expected.length) rowActions.push(["expected", "Record a scheduled settlement"]);
      if (setup.accounts.length > 1) rowActions.push(["transfer", "Internal transfer"]);
      if (setup.people.length) rowActions.push(["settle", "Record a shared settlement"]);
    }
    const treatment = select(rowActions, row.action);
    const extra = element("div", "imports-row-extra");
    values.append(label("Posting date", date), label("Description", name), label("Signed amount (€)", amount), label("Action", treatment));
    for (const node of [date, name, amount, treatment]) node.disabled = readOnly;
    date.addEventListener("input", () => { row.posted_on = date.value; updateRow(); });
    name.addEventListener("input", () => { row.name = name.value; updateRow(); });
    date.addEventListener("change", updateSpending);
    name.addEventListener("change", updateSpending);
    amount.addEventListener("change", () => {
      row.value = amount.value;
      row.category_id = "";
      actionFields(row, extra, updateAction);
      updateSpending();
    });
    amount.addEventListener("input", () => { row.value = amount.value; updateRow(); });
    const updateAction = () => { updateRow(); updateSpending(); };
    treatment.addEventListener("change", () => {
      row.action = treatment.value;
      actionFields(row, extra, updateAction);
      updateAction();
    });
    if (!readOnly) actionFields(row, extra, updateAction);
    section.append(head, comparison, values, extra);
    if (row.currency_unconfirmed) {
      const currency = element("input");
      currency.type = "checkbox";
      currency.checked = row.currency_confirmed;
      currency.disabled = readOnly;
      currency.addEventListener("change", () => {
        row.currency_confirmed = currency.checked;
        if (!row.currency_confirmed) row.selected = pick.checked = false;
        updateRow();
        updateSpending();
      });
      const confirm = element("label", "imports-check imports-row-currency");
      confirm.append(currency, "Confirm this row is in euros");
      section.append(confirm);
    }
    updateRow();
    if (rowSource) {
      const evidence = element("details", "imports-row-source");
      evidence.append(element("summary", "", sourceLabel(row)));
      for (const origin of row.origins) {
        const heading = origin.file + (origin.page ? " · Page " + origin.page : "");
        evidence.append(element("p", "muted", heading), element("p", "", origin.evidence || origin.description));
      }
      section.append(evidence);
    }
    if (row.overlap || row.matchOverlap) section.append(element("p", "muted", row.overlap || row.matchOverlap));
    if (row.reference) section.append(element("p", "muted", "Reference " + row.reference));
    if (row.errors.length && !row.existing) section.append(element("p", "muted", row.errors.join(" ")));
    if (!row.matches.some(item => !item.reconciled) && row.matches.length && !row.existing) {
      section.append(element("p", "muted", "A similar transaction already matches another row in this source."));
    }
    return section;
  }

  function makePayload(row) {
    const [rule, due] = row.commitment.split("|");
    return {
      token: row.token, line: row.line, action: row.action, amount: row.value,
      name: row.name, posted_on: row.posted_on, category_id: row.category_id,
      entry_id: row.entry_id ? Number(row.entry_id) : null, rule_id: rule, due_on: due,
      expected_id: row.expected_id, other_account_id: row.other_account_id, person_id: row.person_id,
      currency_confirmed: row.currency_unconfirmed && row.currency_confirmed ? true : undefined,
    };
  }

  async function commit(readyOnly = false) {
    if (busy) return;
    const rows = result.rows.filter(row => readyOnly ? isBulkReady(row) : row.selected);
    if (!rows.length) return;
    setStatus();
    if (result.source === "pdf") {
      const fingerprints = rows.filter(row => row.review_key).map(row => row.review_key);
      const ledgerLinks = rows.filter(row => row.action === "link" && row.entry_id).map(row => row.entry_id);
      if (new Set(fingerprints).size !== fingerprints.length || new Set(ledgerLinks).size !== ledgerLinks.length) {
        setStatus("Select one row for each repeated statement transaction or ledger match.");
        status.scrollIntoView({ block: "nearest" });
        return;
      }
    }
    setBusy(true);
    renderPreview();
    let added = 0, linked = 0, other = 0, already = 0;
    let failed;
    try {
      for (let start = 0; start < rows.length; start += commitSize) {
        const batch = rows.slice(start, start + commitSize);
        const response = await api(endpoint + "commit", { method: "POST", data: {
          rows: batch.map(makePayload), ready_only: readyOnly, name: result.name,
        } });
        for (const [index, record] of response.rows.entries()) {
          const row = batch[index];
          if (record.existing) already++;
          else if (record.action === "link") linked++;
          else if (record.action === "create") added++;
          else other++;
          row.existing = record.entry_id;
          row.appliedLabel = record.existing ? "Already handled"
            : record.action === "link" ? "Linked to existing transaction"
              : record.action === "create" ? "New transaction recorded" : "Applied";
          row.review_group = "handled";
          row.selected = false;
        }
      }
    } catch (error) {
      failed = error;
    }
    if (added + linked + other + already) {
      notify([added && countLabel(added, "new transaction") + " recorded", linked && countLabel(linked, "existing transaction") + " linked",
        other && countLabel(other, "other row") + " applied", already && already + " already handled"]
        .filter(Boolean).join(" · ") + ".");
      await open();
      onChange();
    }
    setBusy(false);
    renderPreview();
    if (failed) {
      showError(failed);
      status.scrollIntoView({ block: "nearest" });
    } else resultBox.querySelector(".imports-groups [aria-pressed=true]")?.focus({ preventScroll: true });
  }

  function renderPdfFiles(files, feedback) {
    const list = element("ul", "imports-statement-files");
    const totalsStatus = {
      matched: "Extracted debits and credits agree with the printed totals.",
      mismatch: "Extracted debits or credits differ from the printed totals.",
      partial: "Only one printed total could be checked.",
      not_printed: "No printed debit and credit totals were found.",
    };
    const documentTypes = { statement: "Statement", paid_invoice: "Paid invoice", unpaid_invoice: "Unpaid invoice" };
    for (const file of files) {
      const item = element("li", "imports-statement-file");
      const details = element("div", "imports-statement-file-detail");
      details.append(element("strong", "", file.filename));
      if (file.error) {
        details.append(element("p", "muted", "Extraction failed. " + file.error));
      } else if (file.duplicate) {
        details.append(element("p", "muted", "This file is already represented in this review."));
      } else {
        const count = countLabel(file.count, "row") + " extracted";
        const totals = file.extraction;
        const type = documentTypes[totals?.document_type];
        details.append(element("p", "muted", count + (type ? " · " + type : "")));
        if (totals) {
          const check = totalsStatus[totals.totals_check];
          if (check) details.append(element("p", "muted", check));
          const amount = cents => money(cents, { fixed: true });
          details.append(element("p", "imports-statement-totals",
            "Debits extracted " + amount(totals.extracted_debits) + " · printed " + amount(totals.printed_debits)),
          element("p", "imports-statement-totals",
            "Credits extracted " + amount(totals.extracted_credits) + " · printed " + amount(totals.printed_credits)));
          if (totals.note) details.append(element("p", "muted", totals.note));
        }
        if (file.notices.length) {
          const notices = element("details");
          notices.append(element("summary", "", "Extraction notes"));
          for (const note of file.notices) notices.append(element("p", "muted", note));
          details.append(notices);
        }
      }
      item.append(details);
      const actions = element("div", "imports-statement-file-actions");
      if (file.document) actions.append(originalAction(file.document, feedback));
      if (file.extracted) actions.append(action("Download extracted text", () =>
        download(file.extracted, file.filename.replace(/\.pdf$/i, "") + "-extracted.txt", "text/plain;charset=utf-8")));
      item.append(actions);
      list.append(item);
    }
    return list;
  }

  function renderPreview() {
    if (!result) return;
    const section = panel("Review inbox");
    previewName = element("p", "imports-batch-name");
    previewName.textContent = previewLabel(result);
    section.append(previewName);
    for (const notice of result.notices) section.append(element("p", "muted imports-explanation", notice));
    const downloadStatus = element("p", "muted imports-download-status");
    downloadStatus.setAttribute("role", "status");
    const spending = element("section", "imports-spending");
    const updateSpending = () => { if (result.source === "pdf") renderPdfSpending(spending, result.rows); };
    if (result.source === "pdf") {
      section.append(renderPdfFiles(result.files, downloadStatus), downloadStatus);
      if (result.rows.length) {
        updateSpending();
        section.append(spending);
      }
    }
    const ready = element("div", "imports-ready");
    const readyDetail = element("p", "", "");
    readyDetail.setAttribute("aria-live", "polite");
    const approve = action("Apply ready rows", () => commit(true), "primary");
    ready.append(readyDetail, approve);
    const toolbar = element("div", "imports-toolbar");
    const selected = element("span", "muted");
    selected.setAttribute("aria-live", "polite");
    const submit = action("Apply selected rows", () => commit(), "primary");
    const clear = action("Clear selection", () => {
      for (const row of result.rows) row.selected = false;
      renderPreview();
    });
    const grouped = Object.fromEntries(Object.keys(reviewGroups).map(key => [key, []]));
    for (const row of result.rows) grouped[row.review_group].push(row);
    if (!grouped[group].length) {
      group = Object.keys(reviewGroups).find(key => grouped[key].length) ?? "ready";
      page = 0;
    }
    const updateCount = () => {
      const count = result.rows.filter(row => row.selected).length;
      const summary = count + " selected";
      if (selected.textContent !== summary) selected.textContent = summary;
      submit.disabled = busy || !count;
      clear.disabled = busy || !count;
      const eligible = result.rows.filter(isBulkReady);
      const linked = eligible.filter(row => row.action === "link").length;
      const added = eligible.length - linked;
      const available = countLabel(added, "new transaction") + " · " + countLabel(linked, "verified match", "verified matches");
      if (readyDetail.textContent !== available) readyDetail.textContent = available;
      ready.hidden = !eligible.length;
      approve.disabled = busy || !eligible.length;
      submit.className = (eligible.length ? "secondary" : "primary") + " imports-apply-selection";
    };
    if (result.rows.length) {
      section.append(ready);
      const navigation = element("div", "imports-groups");
      navigation.setAttribute("role", "group");
      navigation.setAttribute("aria-label", "Import review groups");
      for (const [key, [name]] of Object.entries(reviewGroups)) {
        const tab = action(name + " · " + grouped[key].length, () => {
          group = key;
          page = 0;
          renderPreview();
          resultBox.querySelector("[aria-pressed=true]")?.focus();
        });
        tab.setAttribute("aria-pressed", String(group === key));
        tab.disabled = busy || !grouped[key].length;
        navigation.append(tab);
      }
      section.append(navigation, element("p", "muted imports-explanation", reviewGroups[group][1]));
      if (result.source === "pdf") {
        const extractedRows = result.rows.filter(canSelectPdfRow);
        if (extractedRows.length) {
          const selectPdf = action("Select " + countLabel(extractedRows.length, "extracted PDF row"), () => {
            for (const row of result.rows.filter(canSelectPdfRow)) row.selected = true;
            renderPreview();
            resultBox.querySelector(".imports-apply-selection")?.focus();
          });
          selectPdf.disabled = busy;
          toolbar.append(selectPdf);
        }
      }
      toolbar.append(clear, action("Download normalized CSV", () =>
        download(normalizedCsv(result.rows), "normalized-payments.csv", "text/csv;charset=utf-8")));
    }
    if (result.source === "pdf" && result.rows.length) toolbar.append(action("Download normalized JSON", () =>
      download(normalizedPdfJson(result), "statement-transactions.json", "application/json")));
    if (result.rows.length) toolbar.append(selected, submit);
    section.append(toolbar);
    const rows = grouped[group];
    const pages = Math.ceil(rows.length / pageSize);
    page = Math.max(0, Math.min(page, pages - 1));
    for (const row of rows.slice(page * pageSize, (page + 1) * pageSize)) section.append(rowNode(row, updateCount, updateSpending));
    if (pages > 1) {
      const pagination = element("div", "pagination");
      const previous = action("Previous", () => { page--; renderPreview(); });
      const next = action("Next", () => { page++; renderPreview(); });
      previous.disabled = page === 0 || busy;
      next.disabled = page + 1 === pages || busy;
      pagination.append(previous, element("span", "", "Page " + (page + 1) + " of " + pages), next);
      section.append(pagination);
    }
    updateCount();
    resultBox.replaceChildren(section);
  }

  source.addEventListener("change", () => {
    file.value = "";
    pdfMethod.value = "local";
    resetAiDraft();
    syncSource();
  });
  file.addEventListener("change", () => { resetConsent(); discardPreview(true); updateAvailability(); });
  pdfMethod.addEventListener("change", () => { resetAiDraft(); syncSource(); });
  consent.addEventListener("change", updateAvailability);
  apiKey.addEventListener("input", updateAvailability);
  importName.addEventListener("input", () => {
    if (!result) return;
    result.name = effectiveName();
    previewName.textContent = previewLabel(result);
  });
  delimiters.addEventListener("change", () => { discardPreview(true); updateAvailability(); });
  account.addEventListener("change", () => { discardPreview(); updateAvailability(); });
  dates.addEventListener("change", () => discardPreview());
  ocr.addEventListener("change", () => { resetConsent(); discardPreview(); updateAvailability(); });
  amountMode.addEventListener("change", () => { discardPreview(); renderMapping(lastSamples); });
  syncSource(true);
  return {
    open,
    pendingReview: () => result?.rows.filter(row => row.review_group !== "handled").length ?? 0,
  };
}
