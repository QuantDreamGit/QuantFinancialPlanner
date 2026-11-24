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
const csvOpenAi = {
  ...pdfProviders.openai,
  consent: "Send the CSV column names and up to eight sample rows to OpenAI to identify the statement layout.",
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
const matchFields = { link: "entry_id", commitment: "commitment", expected: "expected_id" };
const targetFields = { ...matchFields, transfer: "other_account_id", settle: "person_id" };

function editable(row) {
  return row.existing == null && !row.blocked;
}

function included(row) {
  return editable(row) && row.action !== "skip";
}

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
  const match = /^([+-]?)(\d+)(?:[.,](\d{1,2}))?$/.exec(value.trim());
  if (!match) return null;
  const magnitude = BigInt(match[2]) * 100n + BigInt((match[3] ?? "").padEnd(2, "0"));
  return match[1] === "-" ? -magnitude : magnitude;
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
      row.action = "";
    }
  }
  return merged;
}

function previewLabel(result) {
  const title = result.name || sourceNames[result.source] || "Statement";
  const model = result.openai_model ? " · " + result.openai_model : "";
  if (result.csv_method === "openai") return title + " · OpenAI CSV review" + model;
  const provider = pdfProviders[result.pdf_method];
  return title + (provider ? " · Extracted with " + provider.name + model : "");
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
  const status = element("p", "notice imports-status");
  status.hidden = true;
  status.tabIndex = -1;
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
  const csvMethod = select([["local", "Manual column mapping"], ["openai", "AI with OpenAI"]], "local");
  const csvMethodLabel = label("CSV import method", csvMethod);
  const aiPanel = element("div", "imports-ai");
  aiPanel.hidden = true;
  const openaiModel = select([], "");
  const modelLabel = label("OpenAI model", openaiModel);
  modelLabel.classList.add("imports-ai-model");
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
  aiPanel.append(modelLabel, keyState, keyPanel, consentLabel, consentDetails);
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
    label("Import name (optional)", importName), pdfMethodLabel, csvMethodLabel, delimiterLabel, dateLabelNode, modeLabel);
  settings.append(grid, info, ocrLabel, aiPanel, mappingPanel, settingsActions);
  const resultBox = element("div");
  resultBox.hidden = true;
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

  let setup, headers, mapping = {}, sourceFile, encoded, result, previewName, busy = false, page = 0;
  let archiveRequest;
  let editingKey = false;
  let promptUndo;
  let categoryRequest = 0, appliedCategoryRequest = 0;
  let categoryMap = new Map();
  let updateDraftCategories;
  const controls = [source, account, file, importName, pdfMethod, csvMethod, openaiModel, consent, apiKey, delimiters, dates, amountMode, ocr];
  const promptPanel = element("details", "imports-prompt");
  promptPanel.append(element("summary", "", "Edit table with a prompt"));
  const promptForm = element("div", "imports-prompt-form");
  const prompt = element("textarea");
  prompt.maxLength = 2000;
  prompt.rows = 2;
  prompt.placeholder = "Example: Use DD-MM-YYYY for missing dates.";
  const promptModel = select([], "");
  const promptKey = textInput("", "OpenAI API key for table edits", "password");
  promptKey.autocomplete = "off";
  const promptKeyLabel = label("OpenAI API key", promptKey);
  const promptKeyHint = element("small", "", "Quant saves this key after a successful OpenAI response.");
  promptKeyLabel.append(promptKeyHint);
  const promptStatus = element("p", "muted");
  promptStatus.setAttribute("role", "status");
  promptStatus.tabIndex = -1;
  const promptApply = action("Send to OpenAI and edit draft", editWithPrompt, "secondary");
  const undoEdit = action("Undo last prompt edit", undoPrompt, "text-button");
  const promptActions = element("div", "imports-review-controls");
  promptActions.append(promptApply, undoEdit);
  const promptSettings = element("div", "imports-prompt-settings");
  promptSettings.append(label("Model for table edits", promptModel), promptKeyLabel);
  promptForm.append(label("Describe your edits", prompt), promptSettings,
    element("p", "muted", "This sends your prompt, editable rows and their source details, and available category and match options to OpenAI."),
    promptActions, promptStatus);
  promptPanel.append(promptForm);
  prompt.addEventListener("input", updatePromptControls);
  promptKey.addEventListener("input", updatePromptControls);
  function setStatus(text = "", tone = "") {
    status.textContent = text;
    status.hidden = !text;
    status.dataset.tone = tone;
  }

  function showError(error) {
    setStatus(error.name === "Error" ? error.message : apiMessage(error));
  }

  function aiProvider() {
    if (source.value === "pdf") return pdfProviders[pdfMethod.value];
    if (source.value === "csv" && csvMethod.value === "openai") return csvOpenAi;
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
    const provider = aiProvider();
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
    const provider = aiProvider();
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
    const provider = aiProvider();
    const manualCsv = source.value === "csv" && !provider;
    read.hidden = !manualCsv;
    const missingPdf = source.value === "pdf" && pdfMethod.value !== "openai" && setup && !setup.pdf_enabled;
    const keyUnavailable = keyAvailability();
    const needsConsent = Boolean(provider && (!consent.checked || keyUnavailable));
    const fileCount = file.files.length;
    read.disabled = busy || !fileCount;
    review.disabled = busy || !setup || !fileCount || !account.value || !!missingPdf
      || needsConsent || (manualCsv && !headers);
    review.textContent = busy ? "Processing…" : provider === csvOpenAi ? "Map and review with OpenAI"
      : provider ? "Extract and review with " + provider.name : "Review transactions";
  }

  function syncSource(initial = false) {
    const isCsv = source.value === "csv";
    const isPdf = source.value === "pdf";
    const provider = aiProvider();
    const directPdf = isPdf && pdfMethod.value === "openai";
    modeLabel.hidden = dateLabelNode.hidden = source.value === "amazon" || !!provider;
    delimiterLabel.hidden = !isCsv;
    ocrLabel.hidden = !isPdf || directPdf || !setup?.ocr_enabled;
    pdfMethodLabel.hidden = !isPdf;
    csvMethodLabel.hidden = !isCsv;
    aiPanel.hidden = !provider;
    modelLabel.hidden = provider?.settings !== "openai";
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
      info.textContent = provider === csvOpenAi
        ? "OpenAI identifies the columns from a sample. Quant reads the full CSV into an editable review table."
        : "Choose the account the statement belongs to. Map its date, description, and signed amount or debit and credit columns.";
    }
    if (!initial) discardPreview(true);
    updateAvailability();
  }

  function discardPreview(discardMapping = false) {
    resetPrompt();
    result = null;
    previewName = null;
    updateDraftCategories = undefined;
    resultBox.replaceChildren();
    resultBox.hidden = true;
    settings.hidden = false;
    setStatus();
    if (discardMapping) {
      headers = null;
      mapping = {};
      lastSamples = [];
      mappingPanel.replaceChildren();
    }
  }

  function setBusy(flag) {
    busy = flag;
    for (const control of controls) control.disabled = flag;
    updateAvailability();
    updatePromptControls();
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

  function updateCategories({ categories, default_categories }, request) {
    if (request < appliedCategoryRequest) return;
    appliedCategoryRequest = request;
    const changed = JSON.stringify(categories) !== JSON.stringify(setup.categories)
      || JSON.stringify(default_categories) !== JSON.stringify(setup.default_categories);
    setup.categories = categories;
    setup.default_categories = default_categories;
    if (!changed) return;
    categoryMap = new Map(categories.map(item => [item.id, item]));
    updateDraftCategories?.();
  }

  async function refreshCategories() {
    if (!result) return;
    const request = ++categoryRequest;
    try {
      updateCategories(await api(endpoint + "setup"), request);
    } catch {
      // Keep the current choices if refreshing fails.
    }
  }

  async function open({ reportError = true } = {}) {
    const request = ++categoryRequest;
    try {
      const { categories, default_categories, ...next } = await api(endpoint + "setup");
      setup = { ...setup, ...next };
      updateCategories({ categories, default_categories }, request);
      options(openaiModel, Object.entries(setup.openai_models), openaiModel.value || setup.openai_default_model);
      options(promptModel, Object.entries(setup.openai_models), promptModel.value || setup.openai_default_model);
      prompt.placeholder = "Example: Use " + setup.today.split("-").reverse().join("-") + " for missing dates.";
      options(account, setup.accounts.filter(item => item.kind !== "investment").map(item => [item.id, item.name]),
        account.value || setup.accounts.find(item => item.kind !== "investment")?.id,
        "Choose an account");
      history.replaceChildren(...(setup.history.length ? setup.history.map(historyItem)
        : [element("p", "muted", "Reviewed imports will be listed here.")]));
      syncSource(true);
    } catch (error) {
      if (reportError) showError(error);
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
  function useCsvLayout(details) {
    headers = details.headers;
    mapping = details.mapping;
    lastSamples = details.sample;
    if (details.date_format) dates.value = details.date_format;
    if (details.amount_mode) amountMode.value = details.amount_mode;
    else if (mapping.amount === undefined && (mapping.debit !== undefined || mapping.credit !== undefined)) {
      amountMode.value = "separate";
    } else if (amountMode.value === "separate") {
      amountMode.value = "debit-negative";
    }
    renderMapping(lastSamples);
  }

  async function inspect() {
    if (busy) return;
    setStatus();
    setBusy(true);
    try {
      const details = await api(endpoint + "inspect", { method: "POST", data: await fileData() });
      useCsvLayout(details);
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  function normalize(row, fileResult, fileIndex) {
    const candidates = row.matches.filter(candidate => !candidate.reconciled);
    const match = candidates.find(candidate => candidate.id === row.auto_match)
      ?? (candidates.length === 1 ? candidates[0] : null);
    const payment = row.expected.length === 1 ? row.expected[0] : null;
    const commitment = row.commitments.length === 1 ? row.commitments[0] : null;
    const value = row.amount === null ? "" : euros(row.amount);
    return {
      ...row,
      value,
      original: { name: row.name, posted_on: row.posted_on, value },
      ...(fileResult ? { fileIndex, origins: [{
        file: fileResult.filename, archive_id: fileResult.document?.id ?? null,
        page: row.page ?? null, evidence: row.evidence ?? null, line: row.line, description: row.statement_description || row.name,
      }] } : {}),
      currency_confirmed: false,
      action: row.auto_match != null ? "link"
        : candidates.length || row.expected.length || row.commitments.length ? "" : "create",
      entry_id: match ? String(match.id) : "",
      expected_id: payment ? String(payment.id) : "",
      commitment: commitment ? commitment.id + "|" + commitment.date : "",
      other_account_id: "", person_id: "",
      category_id: row.suggested_category_id != null ? String(row.suggested_category_id) : "default",
    };
  }

  async function preview() {
    if (review.disabled) return;
    const provider = aiProvider();
    const key = provider && !setup[provider.settings]?.saved_key ? apiKey.value.trim() : "";
    const approved = consent.checked;
    if (provider) {
      resetConsent();
      apiKey.value = "";
    }
    const name = effectiveName();
    setStatus();
    setBusy(true);
    let loaded = false;
    try {
      const input = { name, account_id: account.value };
      if (!provider) Object.assign(input, { date_format: dates.value, amount_mode: amountMode.value });
      if (provider) input.ai_consent = approved;
      if (provider?.settings === "openai") input.openai_model = openaiModel.value;
      if (source.value === "pdf") {
        input.pdf_method = pdfMethod.value;
        await previewPdfs([...file.files], input, provider, key);
      } else {
        if (provider === csvOpenAi) {
          input.csv_method = "openai";
          if (key) input.api_key = key;
        } else if (source.value === "csv") {
          input.mapping = Object.fromEntries(Object.entries(mapping).filter(([field]) =>
            amountMode.value === "separate" ? !["amount", "direction"].includes(field) : !["debit", "credit"].includes(field)));
        }
        const response = await api(endpoint + "preview", { method: "POST", data: { ...await fileData(), ...input } });
        if (provider && response.ai) setup[provider.settings] = response.ai;
        result = { ...response, name: response.name || name, rows: response.rows.map(row => normalize(row)) };
      }
      editingKey = false;
      page = 0;
      resetPrompt();
      promptModel.value = result.openai_model || openaiModel.value;
      loaded = true;
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
      renderPreview();
      if (loaded) {
        settings.hidden = true;
        resultBox.querySelector("h2").focus();
      }
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
    result = { source: "pdf", name: input.name, pdf_method: input.pdf_method, openai_model: input.openai_model,
      notices: [], files: outcomes, rows: mergePdfRows(rows) };
    setStatus();
    if (documents.size || input.pdf_method === "openai") void loadArchive();
  }

  function rowActions(row) {
    const actions = [["create", result.source === "amazon" ? "Record charge" : "Record new"]];
    if (!row.action) actions.unshift(["", "Choose action"]);
    if (row.matches.some(item => !item.reconciled)) actions.push(["link", "Link existing"]);
    if (result.source !== "amazon") {
      if (row.commitments.length) actions.push(["commitment", "Match commitment"]);
      if (row.expected.length) actions.push(["expected", "Scheduled settlement"]);
      if (setup.accounts.length > 1) actions.push(["transfer", "Internal transfer"]);
      if (setup.people.length) actions.push(["settle", "Shared settlement"]);
    }
    actions.push(["skip", "Skip row"]);
    return actions;
  }

  function actionTargets(row, action) {
    switch (action) {
      case "link": return ["Existing transaction", row.matches.filter(item => !item.reconciled)
        .map(item => [item.id, item.name + " · " + dateLabel(item.posted_on) + " · " + item.kind])];
      case "transfer": return ["Other account", setup.accounts.filter(item => String(item.id) !== account.value)
        .map(item => [item.id, item.name])];
      case "settle": return ["Person", setup.people.map(person => [person.id, person.name + " (@" + person.tag + ")"])];
      case "commitment": return ["Scheduled occurrence", row.commitments.map(item => [item.id + "|" + item.date,
        item.name + " · " + dateLabel(item.date)])];
      case "expected": return ["Scheduled settlement", row.expected.map(item => [item.id, "@" + item.tag + " · " + dateLabel(item.date)])];
      default: return ["", []];
    }
  }

  function setCategoryOptions(control, row) {
    const kind = parsedCents(row.value) < 0n ? "expense" : "income";
    const categories = setup.categories.filter(item => item.kind === kind);
    const defaultName = setup.default_categories[kind];
    const existingDefault = categories.find(item => item.parent_id == null && item.name.toLowerCase() === defaultName.toLowerCase());
    const choices = categories.map(item => [item.id, categoryPath(item, categoryMap)]);
    const defaultId = existingDefault ? String(existingDefault.id) : "default";
    if (!existingDefault) choices.unshift([defaultId, defaultName]);
    const selected = row.category_id === "default" ? defaultId : row.category_id;
    if (selected && !choices.some(([id]) => String(id) === selected)) {
      choices.push([selected, "Category unavailable"]);
    }
    options(control, choices, selected, "No category");
  }

  function actionFields(row, container, rowLabel, updateRow) {
    container.replaceChildren();
    function addChoices(heading, key, pairs) {
      const control = element("select");
      if (key === "category_id") setCategoryOptions(control, row);
      else options(control, pairs, row[key], "Choose " + heading.toLowerCase());
      control.setAttribute("aria-label", heading + " for " + rowLabel);
      control.addEventListener("change", () => { row[key] = control.value; updateRow(); });
      container.append(control);
    }
    const key = targetFields[row.action];
    if (key) {
      const [heading, pairs] = actionTargets(row, row.action);
      addChoices(heading, key, pairs);
    } else if (row.action === "create") {
      addChoices("Category", "category_id");
    }
  }

  function isBulkReady(row) {
    if (row.existing || row.blocked || row.errors.length || row.currency_unconfirmed || row.review_hold
      || row.overlap || row.matchOverlap) return false;
    if (!Object.entries(row.original).every(([field, value]) => row[field] === value)) return false;
    if (row.auto_match != null) return row.action === "link" && row.entry_id === String(row.auto_match);
    return row.review_group === "ready" && row.action === "create";
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
    const suggested = choices.link || choices.commitment || choices.expected;
    const proposed = row.existing != null && reconciled
      ? ["Already linked transaction", reconciled.name, reconciled.posted_on, reconciled]
      : row.action === "" ? suggested : choices[row.action];
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

  function reviewIssue(row) {
    if (!row.action) return "Choose how to record or match this transaction.";
    if (!row.posted_on || row.posted_on > setup.today) return "Enter a date on or before " + dateLabel(setup.today) + ".";
    if (!row.name.trim()) return "Enter a description.";
    const cents = parsedCents(row.value);
    if (!cents || cents <= -(2n ** 63n) || cents >= 2n ** 63n) return "Enter a nonzero euro amount with up to two decimal places.";
    if (row.action === "create" && row.category_id && row.category_id !== "default"
      && categoryMap.get(Number(row.category_id))?.kind !== (cents < 0n ? "expense" : "income")) {
      return "Choose an available category.";
    }
    if (row.currency_unconfirmed && !row.currency_confirmed) return "Confirm the currency is EUR.";
    if (row.action === "create" && result.source === "amazon" && cents > 0n) return "Enter a negative amount for the confirmed charge.";
    if (row.action === "transfer" && !row.other_account_id) return "Choose the other account.";
    if (row.action === "settle" && !row.person_id) return "Choose the person for the settlement.";
    const field = matchFields[row.action];
    if (field && !row[field]) return "Choose the transaction or scheduled payment to match.";
    const match = row.action === "link" ? row.matches.find(item => String(item.id) === row.entry_id)
      : row.action === "commitment" ? row.commitments.find(item => item.id + "|" + item.date === row.commitment)
        : row.action === "expected" ? row.expected.find(item => String(item.id) === row.expected_id) : null;
    if (match && BigInt(match.amount) !== cents) return "The amount differs from the chosen match.";
    return "";
  }

  function draftIssues(rows) {
    const issues = new Map();
    const fingerprints = new Map();
    const targets = new Map();
    function checkDuplicate(index, key, row, issue) {
      if (index.has(key)) {
        issues.set(index.get(key), issue);
        issues.set(row, issue);
      } else {
        index.set(key, row);
      }
    }
    for (const row of rows) {
      const issue = reviewIssue(row);
      if (issue) issues.set(row, issue);
      if (row.review_key) checkDuplicate(fingerprints, row.review_key, row, "Choose Skip row on repeated copies of this transaction.");
      const field = matchFields[row.action];
      if (field && row[field]) {
        checkDuplicate(targets, row.action + "|" + row[field], row, "Another row has the same match. Choose a different action or match.");
      }
    }
    return issues;
  }

  function updatePromptControls() {
    const needsKey = !setup?.openai?.configured;
    promptKeyLabel.hidden = !needsKey;
    promptKeyHint.hidden = !setup?.openai?.can_save_key || setup.openai.saved_key;
    for (const control of [prompt, promptKey, promptModel]) control.disabled = busy;
    promptApply.disabled = busy || !prompt.value.trim() || (needsKey && !promptKey.value.trim());
    promptApply.textContent = busy ? "Working…" : "Send to OpenAI and edit draft";
    undoEdit.hidden = !promptUndo;
    undoEdit.disabled = busy;
  }

  function resetPrompt() {
    promptPanel.open = false;
    prompt.value = promptKey.value = promptStatus.textContent = "";
    promptUndo = undefined;
  }

  function keepManualEdit(row, line) {
    if (!promptUndo?.changes.delete(row)) return;
    promptUndo.preserved++;
    line.classList.remove("imports-prompt-updated");
    if (!promptUndo.changes.size && !promptUndo.reordered) promptUndo = undefined;
    updatePromptControls();
  }

  function promptRow(row, index) {
    const choices = {};
    for (const action of Object.keys(matchFields)) {
      const [, pairs] = actionTargets(row, action);
      if (pairs.length) choices[action] = pairs;
    }
    return {
      id: index + 1, posted_on: row.posted_on, name: row.name, amount: row.value,
      category_id: row.category_id, action: row.action, target: row[targetFields[row.action]] || "",
      currency: reviewedCurrency(row), reference: row.reference, source: sourceLabel(row), line: row.line,
      ...(row.csv_source ? { csv_source: row.csv_source } : {}),
      ...(Object.keys(choices).length ? { choices } : {}),
    };
  }

  function checkPromptChanges(row, edits) {
    const { amount, target, ...changes } = edits;
    const invalid = message => { throw new Error("Row " + row.line + " · " + message); };
    if (changes.posted_on) {
      const parsed = new Date(changes.posted_on + "T00:00:00Z");
      if (!/^(?!0000)\d{4}-\d{2}-\d{2}$/.test(changes.posted_on) || !Number.isFinite(parsed.getTime())
        || parsed.toISOString().slice(0, 10) !== changes.posted_on || changes.posted_on > setup.today) {
        invalid("Enter today’s date or an earlier calendar date.");
      }
    }
    if (changes.name !== undefined && ([...changes.name].length > 100 || /\0/.test(changes.name))) {
      invalid("The proposed description is invalid or longer than 100 characters.");
    }
    if (amount !== undefined) {
      const cents = parsedCents(amount);
      if (amount !== "" && (!cents || cents <= -(2n ** 63n) || cents >= 2n ** 63n || amount.length > 28)) {
        invalid("The proposed amount is invalid.");
      }
      changes.value = amount;
    }
    const category = changes.category_id ?? row.category_id;
    if (category && category !== "default") {
      const kind = parsedCents(amount ?? row.value) < 0n ? "expense" : "income";
      const valid = setup.categories.some(item => String(item.id) === category && item.kind === kind);
      if (!valid && changes.category_id !== undefined) invalid("The proposed category is unavailable for this transaction.");
      if (!valid && amount !== undefined) changes.category_id = "default";
    }
    const action = changes.action ?? row.action;
    if (!rowActions(row).some(([value]) => value === action)) invalid("The proposed action is unavailable.");
    if (target !== undefined) {
      const key = targetFields[action];
      const [, allowed] = actionTargets(row, action);
      if (!key || (target && !allowed.some(([id]) => String(id) === target))) invalid("The proposed match or destination is unavailable.");
      changes[key] = target;
    }
    return changes;
  }

  function orderedRows(rows, sort) {
    const { column, direction } = sort;
    const read = column === "amount" ? row => parsedCents(row.value) : row => row[column];
    return [...rows].sort((first, second) => {
      const a = read(first);
      const b = read(second);
      if (a === null || a === "") return b === null || b === "" ? 0 : 1;
      if (b === null || b === "") return -1;
      const compared = typeof a === "bigint" ? Number(a > b) - Number(a < b) : a.localeCompare(b);
      return direction === "desc" ? -compared : compared;
    });
  }

  async function editWithPrompt() {
    if (promptApply.disabled || !result) return;
    const rows = result.reviewRows ?? result.rows;
    const editableRows = new Map(rows.flatMap((row, index) => editable(row) ? [[index + 1, row]] : []));
    if (!editableRows.size) return;
    const data = {
      ai_consent: true, prompt: prompt.value.trim(), openai_model: promptModel.value,
      source: result.source, account_id: account.value,
      rows: [...editableRows].map(([id, row]) => promptRow(row, id - 1)),
    };
    if (!setup.openai?.configured) data.api_key = promptKey.value.trim();
    setBusy(true);
    promptStatus.textContent = "Preparing edits…";
    renderPreview();
    try {
      const response = await api(endpoint + "review-prompt", { method: "POST", data });
      if (response.ai) setup.openai = response.ai;
      const proposed = new Map();
      for (const { rows: ids, ...fields } of response.updates) {
        for (const id of ids ?? editableRows.keys()) {
          const row = editableRows.get(id);
          if (!row) throw new Error("An edit referred to a row outside the editable draft.");
          const changes = proposed.get(row) ?? {};
          for (const [field, value] of Object.entries(fields)) {
            if (field in changes && changes[field] !== value) throw new Error("The prompt returned conflicting edits for the same cell.");
            changes[field] = value;
          }
          proposed.set(row, changes);
        }
      }
      const updates = new Map();
      const changes = new Map();
      for (const [row, edits] of proposed) {
        const checked = Object.fromEntries(Object.entries(checkPromptChanges(row, edits))
          .filter(([field, value]) => row[field] !== value));
        if (Object.keys(checked).length) {
          updates.set(row, checked);
          changes.set(row, Object.fromEntries(Object.keys(checked).map(field => [field, row[field]])));
        }
      }
      for (const [row, edits] of updates) Object.assign(row, edits);
      const sorted = response.sort ? orderedRows(rows, response.sort) : rows;
      const reordered = sorted.some((row, index) => row !== rows[index]);
      if (changes.size || reordered) {
        promptUndo = { changes, order: result.reviewRows, reordered, preserved: 0 };
        if (reordered) result.reviewRows = sorted;
        const firstChanged = sorted.findIndex(row => changes.has(row));
        page = firstChanged >= 0 ? Math.floor(firstChanged / pageSize) : 0;
        prompt.value = "";
      }
      const count = changes.size ? "Updated " + countLabel(changes.size, "row") + "." : "";
      const summary = [count, reordered ? "Sorted the table." : "",
        !count && !reordered ? response.message || "No draft rows changed." : "", ...response.notices];
      promptStatus.textContent = summary.filter(Boolean).join(" ");
      if (setup.openai?.configured) promptKey.value = "";
    } catch (error) {
      promptStatus.textContent = error.message || apiMessage(error);
    } finally {
      setBusy(false);
      renderPreview();
      promptStatus.focus({ preventScroll: true });
    }
  }

  function undoPrompt() {
    if (busy || !result || !promptUndo) return;
    for (const [row, changes] of promptUndo.changes) Object.assign(row, changes);
    result.reviewRows = promptUndo.order;
    promptStatus.textContent = "Undid the last prompt edit."
      + (promptUndo.preserved ? " Rows you edited afterward kept their changes." : "");
    promptUndo = undefined;
    renderPreview();
    promptStatus.focus({ preventScroll: true });
  }

  function rowNode(row, index, updateDraft, updateSpending) {
    const line = element("tr");
    line.dataset.importRow = index;
    if (promptUndo?.changes.has(row)) line.classList.add("imports-prompt-updated");
    const rowLabel = "row " + row.line + (row.origins?.[0] ? " in " + row.origins[0].file : "");
    const immutable = !editable(row);
    const date = textInput(row.posted_on, "Posting date for " + rowLabel, "date");
    date.max = setup.today;
    const name = textInput(row.name, "Description for " + rowLabel);
    name.maxLength = 100;
    const amount = textInput(row.value, "Signed amount for " + rowLabel);
    amount.inputMode = "decimal";
    amount.maxLength = 28;
    const treatment = select(rowActions(row), row.action);
    treatment.setAttribute("aria-label", "Action for " + rowLabel);
    treatment.disabled = busy;
    const extra = element("td", "imports-review-target");
    const comparison = element("div", "imports-match");
    const notes = [sourceLabel(row), row.csv_source, row.reference && "Reference " + row.reference,
      row.overlap, row.matchOverlap].filter(Boolean);
    const warnings = row.errors.filter(issue => !(row.currency_unconfirmed && issue === "Confirm that this row is in euros."));
    if (row.review_hold) warnings.push("Check the extracted amounts against the totals printed on this statement.");
    if (row.matches.length && !row.matches.some(item => !item.reconciled) && !immutable) {
      warnings.push("A similar transaction already matches another row in this source.");
    }
    const details = element("tr", "imports-review-details");
    const content = element("td");
    content.colSpan = 6;
    const detailBody = element("div", "imports-review-detail-body");
    detailBody.append(element("strong", "", "Source " + rowLabel));
    for (const note of notes) detailBody.append(element("p", "muted", note));
    for (const origin of row.origins ?? []) {
      const excerpt = origin.evidence || origin.description;
      if (excerpt) detailBody.append(element("p", "", row.origins.length > 1 ? origin.file + " · " + excerpt : excerpt));
    }
    if (warnings.length) detailBody.append(element("p", "", "Source notes"), element("p", "muted", warnings.join(" ")));
    detailBody.append(comparison);
    content.append(detailBody);
    details.append(content);
    details.id = "import-row-details-" + index;
    details.hidden = !row.detailsOpen;
    const toggle = action("Details", () => {
      row.detailsOpen = details.hidden;
      details.hidden = !row.detailsOpen;
      toggle.setAttribute("aria-expanded", String(row.detailsOpen));
    }, "text-button");
    toggle.setAttribute("aria-label", "Details for " + rowLabel);
    toggle.setAttribute("aria-controls", details.id);
    toggle.setAttribute("aria-expanded", String(Boolean(row.detailsOpen)));
    toggle.hidden = !notes.length && !warnings.length && !row.matches.length && !row.commitments.length && !row.expected.length;
    const issueNode = element("span", "imports-review-issue");
    issueNode.tabIndex = -1;
    const reviewCell = element("td", "imports-review-state");
    reviewCell.append(issueNode, toggle);
    const updateMatch = () => {
      keepManualEdit(row, line);
      compareMatch(row, comparison);
      updateDraft();
    };
    const updateAction = () => {
      for (const input of [date, name, amount]) input.disabled = immutable || busy || row.action === "skip";
      if (!immutable && row.action !== "skip") {
        actionFields(row, extra, rowLabel, () => { updateMatch(); updateSpending(); });
        const target = extra.querySelector("select");
        if (target && busy) target.disabled = true;
      } else {
        extra.replaceChildren();
      }
    };
    for (const [input, key] of [[date, "posted_on"], [name, "name"], [amount, "value"]]) {
      input.addEventListener("input", () => { row[key] = input.value; updateMatch(); });
      input.addEventListener("change", updateSpending);
    }
    amount.addEventListener("change", () => {
      const category = setup.categories.find(item => String(item.id) === row.category_id);
      if (category && category.kind !== (parsedCents(row.value) < 0n ? "expense" : "income")) {
        row.category_id = "default";
      }
      if (row.action === "create") {
        updateAction();
        updateDraft();
      }
    });
    treatment.addEventListener("change", () => {
      row.action = treatment.value;
      if (treatment.options[0].value === "" && row.action) treatment.options[0].remove();
      updateAction();
      updateMatch();
      updateSpending();
    });
    updateAction();
    compareMatch(row, comparison);
    const choice = immutable ? element("span", "muted", row.blocked ? "Excluded" : "Already handled") : treatment;
    line.append(element("td", "imports-review-date", date), element("td", "imports-review-name", name),
      element("td", "imports-review-amount", amount), element("td", "imports-review-action", choice), extra, reviewCell);
    return { line, details, refreshCategory() {
      const control = extra.querySelector("select");
      if (row.action === "create" && control) setCategoryOptions(control, row);
    }, showIssue(issue) {
      line.classList.toggle("imports-review-excluded", !included(row));
      issueNode.classList.toggle("imports-review-invalid", Boolean(issue));
      const hasWarning = warnings.length || row.overlap || row.matchOverlap;
      issueNode.textContent = issue || (row.blocked ? row.currency + " unsupported"
        : row.existing != null ? row.appliedLabel || ""
          : row.action === "skip" ? "Skipped" : hasWarning ? "Check source" : isBulkReady(row) && row.action === "link" ? "Verified match" : "");
    } };
  }

  function makePayload(row) {
    const [rule, due] = row.commitment.split("|");
    return {
      token: row.token, line: row.line, action: row.action, amount: row.value,
      name: row.name, posted_on: row.posted_on, category_id: row.action === "create" ? row.category_id : "",
      entry_id: row.entry_id ? Number(row.entry_id) : null, rule_id: rule, due_on: due,
      expected_id: row.expected_id, other_account_id: row.other_account_id, person_id: row.person_id,
      currency_confirmed: row.currency_unconfirmed && row.currency_confirmed ? true : undefined,
      verify_ready: isBulkReady(row) ? true : undefined,
    };
  }

  async function commit() {
    if (busy || !result) return;
    const rows = result.rows.filter(included);
    if (draftIssues(rows).size) return;
    const skipped = result.rows.filter(row => row.existing == null && !included(row)).length;
    const failedFiles = result.files?.filter(item => item.error).length ?? 0;
    setStatus(rows.length ? "Importing " + countLabel(rows.length, "transaction") + "…" : "", "progress");
    setBusy(true);
    renderPreview();
    let saved = 0, linked = 0, already = 0;
    try {
      for (let start = 0; start < rows.length; start += commitSize) {
        const batch = rows.slice(start, start + commitSize);
        const response = await api(endpoint + "commit", { method: "POST", data: {
          rows: batch.map(makePayload), name: result.name,
        } });
        for (const [index, record] of response.rows.entries()) {
          const row = batch[index];
          if (record.existing) already++;
          else {
            saved++;
            if (record.action === "link") linked++;
          }
          row.existing = record.entry_id;
          row.appliedLabel = record.existing ? "Already handled"
            : record.action === "link" ? "Linked" : "Imported";
        }
      }
    } catch (error) {
      setBusy(false);
      renderPreview();
      const progress = saved + already;
      setStatus((progress ? countLabel(progress, "row") + " processed before the error. " : "") + apiMessage(error));
      status.focus();
      if (progress) {
        void open({ reportError: false });
        onChange();
      }
      return;
    }
    const handled = result.rows.length - rows.length - skipped + already;
    discardPreview(true);
    importName.value = "";
    file.value = "";
    sourceFile = encoded = null;
    resetConsent();
    setBusy(false);
    const summary = [
      saved ? countLabel(saved, "transaction") + " imported." : "No new transactions to import.",
      linked && countLabel(linked, "existing transaction") + " linked.",
      handled && countLabel(handled, "row") + " already handled.",
      skipped && countLabel(skipped, "row") + " skipped.",
      failedFiles && countLabel(failedFiles, "PDF") + " could not be extracted.",
    ].filter(Boolean).join(" ");
    setStatus(summary, failedFiles ? "warning" : "success");
    status.focus();
    if (saved + already) {
      void open({ reportError: false });
      onChange();
    }
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
    updateDraftCategories = undefined;
    const section = panel("Review transactions");
    const heading = section.firstElementChild;
    heading.tabIndex = -1;
    const header = element("div", "imports-review-header");
    const headingGroup = element("div");
    previewName = element("p", "imports-batch-name");
    previewName.textContent = previewLabel(result);
    headingGroup.append(heading, previewName);
    const editSettings = action("Import settings", () => {
      settings.hidden = false;
      source.focus();
    });
    editSettings.disabled = busy;
    header.append(headingGroup, editSettings);
    section.append(header);
    for (const notice of result.notices) section.append(element("p", "muted imports-explanation", notice));
    const downloadStatus = element("p", "muted imports-download-status");
    downloadStatus.setAttribute("role", "status");
    const spending = element("section", "imports-spending");
    const updateSpending = () => { if (result.source === "pdf") renderPdfSpending(spending, result.rows); };
    const rows = result.reviewRows ?? result.rows;
    const pages = Math.ceil(rows.length / pageSize);
    if (rows.length) {
      section.append(element("p", "muted imports-explanation",
        "Edit cells as needed, then import the table. To leave out a transaction, choose Skip row in its Action menu."));
      if (rows.some(editable)) {
        updatePromptControls();
        section.append(promptPanel);
      }
      const table = element("table", "imports-review-table");
      table.setAttribute("aria-label", "Transactions to review");
      const tableHeading = element("tr");
      for (const name of ["Posting date", "Description", "Amount (€)", "Action", "Category or match", "Review"]) {
        const cell = element("th", "", name);
        cell.scope = "col";
        tableHeading.append(cell);
      }
      const body = element("tbody");
      table.append(element("thead", "", tableHeading), body);
      const scroll = element("div", "table-scroll imports-review-scroll", table);
      scroll.setAttribute("role", "region");
      scroll.setAttribute("aria-label", "Editable import table");
      scroll.tabIndex = 0;
      const toolbar = element("div", "imports-review-toolbar");
      const summary = element("p", "imports-review-count");
      summary.setAttribute("role", "status");
      const historyNotice = element("p", "muted imports-explanation",
        "Importing the earlier transactions will move the affected accounts’ opening dates back. Their opening balances will apply before those transactions.");
      const openingDates = new Map(setup.accounts.map(item => [String(item.id), item.opened_on]));
      const controls = element("div", "imports-review-controls");
      const submit = action("", commit, "primary");
      const confirmCurrency = action("", () => {
        for (const row of rows.filter(included)) {
          if (row.currency_unconfirmed) row.currency_confirmed = true;
        }
        updateDraft();
        updateSpending();
        (submit.disabled ? firstIssue : submit).focus();
      });
      const firstIssue = action("Go to first issue", () => {
        const index = rows.findIndex(row => issues.has(row));
        const targetPage = Math.floor(index / pageSize);
        if (targetPage !== page) {
          page = targetPage;
          renderPreview();
        }
        resultBox.querySelector('[data-import-row="' + index + '"] .imports-review-issue')?.focus();
      });
      controls.append(confirmCurrency, firstIssue, submit);
      toolbar.append(summary, controls);
      const start = page * pageSize;
      const visible = rows.slice(start, start + pageSize);
      const entries = visible.map((row, i) => rowNode(row, start + i, updateDraft, updateSpending));
      for (const entry of entries) body.append(entry.line, entry.details);
      let issues;
      function updateDraft() {
        const pending = rows.filter(included);
        issues = draftIssues(pending);
        historyNotice.hidden = !pending.some(row =>
          row.action && row.action !== "link" && row.posted_on && (row.posted_on < openingDates.get(account.value)
            || (row.action === "transfer" && row.posted_on < openingDates.get(row.other_account_id))));
        const unknown = pending.filter(row => row.currency_unconfirmed && !row.currency_confirmed).length;
        const handled = rows.filter(row => row.existing != null).length;
        const skipped = rows.length - pending.length - handled;
        const count = [countLabel(pending.length, "transaction") + " to import",
          issues.size && (issues.size === 1 ? "1 needs attention" : issues.size + " need attention"),
          handled && handled + " already handled", skipped && skipped + " skipped"]
          .filter(Boolean).join(" · ");
        if (summary.textContent !== count) summary.textContent = count;
        confirmCurrency.hidden = !unknown;
        confirmCurrency.disabled = busy;
        confirmCurrency.textContent = "Confirm EUR for " + countLabel(unknown, "row");
        firstIssue.hidden = !issues.size;
        firstIssue.disabled = busy;
        submit.disabled = busy || issues.size > 0;
        submit.textContent = busy ? "Importing…" : pending.length ? "Import " + countLabel(pending.length, "transaction") : "Finish review";
        for (const [i, entry] of entries.entries()) entry.showIssue(issues.get(visible[i]));
      }
      updateDraft();
      updateDraftCategories = () => {
        for (const entry of entries) entry.refreshCategory();
        updateDraft();
      };
      section.append(historyNotice, toolbar, scroll);
      if (pages > 1) {
        const pagination = element("div", "pagination");
        const range = element("span", "", (start + 1) + "–" + (start + visible.length) + " of " + rows.length);
        range.tabIndex = -1;
        const changePage = delta => {
          page += delta;
          renderPreview();
          resultBox.querySelector(".pagination span")?.focus();
        };
        const previous = action("Previous", () => changePage(-1));
        const next = action("Next", () => changePage(1));
        previous.disabled = page === 0 || busy;
        next.disabled = page + 1 === pages || busy;
        pagination.append(previous, range, next);
        section.append(pagination);
      }
      const exports = element("div", "imports-toolbar");
      exports.append(action("Download normalized CSV", () =>
        download(normalizedCsv(rows), "normalized-payments.csv", "text/csv;charset=utf-8")));
      if (result.source === "pdf") exports.append(action("Download normalized JSON", () =>
        download(normalizedPdfJson(result), "statement-transactions.json", "application/json")));
      section.append(exports);
    } else {
      section.append(element("p", "muted imports-explanation", "No transactions were found."));
    }
    if (result.source === "pdf") {
      const files = element("details", "imports-review-files");
      const failed = result.files.filter(item => item.error).length;
      const mismatches = result.files.filter(item => item.extraction?.totals_check === "mismatch").length;
      const fileSummary = [countLabel(result.files.length, "source PDF"),
        failed && failed + " failed to extract", mismatches && countLabel(mismatches, "totals mismatch", "totals mismatches")]
        .filter(Boolean).join(" · ");
      files.open = Boolean(failed || mismatches || !rows.length);
      files.append(element("summary", "", fileSummary), renderPdfFiles(result.files, downloadStatus), downloadStatus);
      section.append(files);
      if (rows.length) {
        const overview = element("details", "imports-review-files");
        overview.append(element("summary", "", "PDF spending summary"), spending);
        section.append(overview);
        updateSpending();
      }
    }
    resultBox.replaceChildren(section);
    resultBox.hidden = false;
  }

  source.addEventListener("change", () => {
    file.value = "";
    pdfMethod.value = "local";
    csvMethod.value = "local";
    resetAiDraft();
    syncSource();
  });
  file.addEventListener("change", () => { resetConsent(); discardPreview(true); updateAvailability(); });
  pdfMethod.addEventListener("change", () => { resetAiDraft(); syncSource(); });
  csvMethod.addEventListener("change", () => {
    const suggested = result?.csv_layout;
    resetAiDraft();
    syncSource();
    if (csvMethod.value === "local" && suggested) {
      useCsvLayout(suggested);
      updateAvailability();
    }
  });
  openaiModel.addEventListener("change", () => discardPreview());
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
    refreshCategories,
    pendingReview: () => result?.rows.filter(included).length ?? 0,
  };
}
