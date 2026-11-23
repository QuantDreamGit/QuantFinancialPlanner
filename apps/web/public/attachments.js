import { api, apiMessage, button, element, fileToBase64 } from "./ui.js";

const directory = id => "/api/transactions/" + id + "/attachments";
const allowed = ".pdf,.png,.jpg,.jpeg,.gif,.webp,.heic,.heif,.tif,.tiff,.bmp,.avif,.txt,.csv,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.odt,.ods,.odp,.rtf";
const maxSize = 10 * 1024 * 1024;
const maxFiles = 12;
const extensions = new Set(allowed.split(","));

function displaySize(size) {
  if (size < 1024) return size + " B";
  const mib = size >= 1024 * 1024;
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(size / (mib ? 1024 ** 2 : 1024))
    + (mib ? " MB" : " KB");
}

function saveDownload(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = element("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function attachmentPanel(root, { pending, target, notify, onChange, onBusy, onPendingChange = () => {} }) {
  const heading = element("h3", "", "Attachments");
  const picker = element("input");
  picker.type = "file";
  picker.multiple = true;
  picker.accept = allowed;
  const label = element("label", "field");
  label.append(element("span", "", "Attach documents"), picker,
    element("small", "", "PDFs, images and common documents. Up to 10 MB per file."));
  const list = element("ul", "attachment-list");
  const status = element("p", "attachment-status muted");
  status.setAttribute("role", "status");
  status.hidden = true;
  root.classList.add("attachments");
  root.replaceChildren(heading, label, list, status);
  const request = new AbortController();
  let documents = [];
  let disabled = false;
  let revision = 0;

  function note(message = "") {
    status.textContent = message;
    status.hidden = !message;
  }

  function refresh() {
    picker.disabled = disabled;
    list.replaceChildren();
    list.hidden = !documents.length && !pending.length;
    for (const item of documents) {
      const row = element("li", "attachment-row");
      const info = element("div", "attachment-name");
      info.append(element("strong", "", item.name), element("small", "muted", displaySize(item.size)));
      const actions = element("div", "attachment-actions");
      const download = button(item.name.toLowerCase().endsWith(".pdf") ? "Download PDF" : "Download",
        "text-button", "", "Download " + item.name);
      const remove = button("Remove", "text-button", "", "Remove " + item.name);
      download.disabled = remove.disabled = disabled;
      download.addEventListener("click", async () => {
        download.disabled = true;
        try {
          const blob = await api(directory(target.id) + "/" + item.id, {
            workspace: target.workspace, responseType: "blob", signal: request.signal,
          });
          if (!request.signal.aborted) saveDownload(blob, item.name);
        } catch (error) {
          if (!request.signal.aborted) note(apiMessage(error));
        } finally {
          download.disabled = disabled;
        }
      });
      remove.addEventListener("click", async () => {
        if (!window.confirm("Remove " + item.name + " from this transaction?")) return;
        onBusy(true);
        try {
          await api(directory(target.id) + "/" + item.id, { method: "DELETE", workspace: target.workspace });
          revision++;
          documents = documents.filter(file => file.id !== item.id);
          note();
          notify("Attachment removed.");
          onChange();
        } catch (error) {
          note(apiMessage(error));
        } finally {
          onBusy(false);
        }
      });
      actions.append(download, remove);
      row.append(info, actions);
      list.append(row);
    }
    for (const file of pending) {
      const row = element("li", "attachment-row");
      const info = element("div", "attachment-name");
      info.append(element("strong", "", file.name),
        element("small", "muted", displaySize(file.size) + " · Ready to upload"));
      const remove = button("Remove", "text-button", "", "Remove pending " + file.name);
      remove.disabled = disabled;
      remove.addEventListener("click", () => {
        pending.splice(pending.indexOf(file), 1);
        note();
        refresh();
        onPendingChange();
      });
      row.append(info, remove);
      list.append(row);
    }
  }

  picker.addEventListener("change", () => {
    const files = [...picker.files];
    picker.value = "";
    note();
    if (files.some(file => !file.size || file.size > maxSize)) {
      note("Choose nonempty files of up to 10 MB each.");
      return;
    }
    if (files.some(file => !extensions.has("." + file.name.split(".").at(-1).toLowerCase()))) {
      note("Choose a supported PDF, image or document.");
      return;
    }
    if (documents.length + pending.length + files.length > maxFiles) {
      note("A transaction can have up to 12 attachments.");
      return;
    }
    pending.push(...files);
    refresh();
    onPendingChange();
  });

  async function load() {
    if (!target) return;
    const started = revision;
    note("Loading files…");
    try {
      const result = await api(directory(target.id), { workspace: target.workspace, signal: request.signal });
      if (request.signal.aborted || started !== revision) return;
      documents = result.attachments;
      refresh();
      note();
    } catch (error) {
      if (!request.signal.aborted) note(apiMessage(error));
    }
  }

  async function upload(saved) {
    target = saved;
    revision++;
    while (pending.length) {
      const pendingFile = pending[0];
      note("Uploading " + pendingFile.name + "…");
      const item = await api(directory(saved.id), {
        method: "POST", workspace: saved.workspace,
        data: { name: pendingFile.name, file: await fileToBase64(pendingFile) },
      });
      if (!documents.some(file => file.id === item.id)) documents.push(item);
      pending.shift();
      refresh();
      onPendingChange();
    }
    note();
  }

  refresh();
  load();
  return {
    upload,
    stop: () => request.abort(),
    focus: () => picker.focus(),
    setBusy: next => {
      disabled = next;
      refresh();
    },
  };
}

export function transactionFilesDialog({ notify, onChange }) {
  const dialog = element("dialog", "editor transaction-files-dialog");
  dialog.setAttribute("aria-labelledby", "transaction-files-title");
  const title = element("h2");
  title.id = "transaction-files-title";
  const close = button("", "icon-link", "close", "Close files");
  const heading = element("div", "dialog-heading");
  heading.append(title, close);
  const panel = element("div");
  const issue = element("p", "notice");
  issue.setAttribute("role", "alert");
  issue.hidden = true;
  const done = button("Done", "secondary");
  const save = button("Save files", "primary");
  const actions = element("div", "dialog-actions", element("div", "dialog-save"));
  actions.firstElementChild.append(done, save);
  dialog.append(heading, issue, panel, actions);
  document.body.append(dialog);
  let widget, target, pending = [];
  let busy = false;

  function updateSave() {
    save.disabled = busy || !pending.length;
  }

  function setBusy(value) {
    busy = value;
    widget?.setBusy(value);
    close.disabled = done.disabled = busy;
    updateSave();
  }

  function dismiss() {
    if (!busy) dialog.close();
  }

  close.addEventListener("click", dismiss);
  done.addEventListener("click", dismiss);
  dialog.addEventListener("cancel", event => {
    event.preventDefault();
    dismiss();
  });
  dialog.addEventListener("close", () => {
    if (!dialog.open) {
      widget?.stop();
      widget = null;
      pending = [];
    }
  });
  save.addEventListener("click", async () => {
    if (busy || !pending.length) return;
    issue.hidden = true;
    setBusy(true);
    try {
      await widget.upload(target);
      notify("Files saved.");
    } catch (error) {
      issue.textContent = apiMessage(error);
      issue.hidden = false;
    } finally {
      setBusy(false);
      onChange();
    }
  });
  return entry => {
    widget?.stop();
    pending = [];
    target = { id: entry.id };
    title.textContent = "Files for " + entry.name;
    issue.hidden = true;
    widget = attachmentPanel(panel, {
      pending, target, notify, onChange, onBusy: setBusy, onPendingChange: updateSave,
    });
    updateSave();
    if (!dialog.open) dialog.showModal();
    if (!entry.attachment_count) widget.focus();
  };
}
