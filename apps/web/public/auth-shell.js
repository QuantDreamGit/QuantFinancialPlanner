import { $, apiMessage, button, element, icon } from "./ui.js";
import { authRequest, signInHref } from "./auth.js";

function formField(label, autocomplete, name) {
  const field = element("label", "field");
  const input = element("input");
  input.type = "password";
  input.name = name;
  input.autocomplete = autocomplete;
  input.required = true;
  field.append(element("span", "", label), input);
  return field;
}

function showError(output, issue) {
  output.textContent = apiMessage(issue);
  output.hidden = false;
}

function makeAlert(id) {
  const alert = element("p", "notice");
  if (id) alert.id = id;
  alert.setAttribute("role", "alert");
  alert.hidden = true;
  return alert;
}

function passwordDialog(trigger, notify) {
  const dialog = element("dialog", "editor auth-password-editor");
  const heading = element("h2", "", "Change password");
  heading.id = "auth-password-heading";
  dialog.setAttribute("aria-labelledby", heading.id);
  const close = button("", "icon-link", "close", "Close dialog");
  close.addEventListener("click", () => dialog.close());
  const top = element("div", "dialog-heading");
  top.append(heading, close);
  const error = makeAlert("auth-password-error");
  const form = element("form");
  form.method = "post";
  form.action = "/auth";
  const save = button("Update password", "primary");
  save.type = "submit";
  const cancel = button("Cancel");
  cancel.addEventListener("click", () => dialog.close());
  const buttons = element("div", "dialog-save");
  buttons.append(cancel, save);
  const actions = element("div", "dialog-actions", buttons);
  form.append(formField("Current password", "current-password", "current_password"),
    formField("New password", "new-password", "password"),
    formField("Confirm new password", "new-password", "confirmation"), actions);
  const fields = form.elements;
  let saving = false;

  function validate() {
    fields.confirmation.setCustomValidity(fields.confirmation.value !== fields.password.value
      ? "Enter the same password again." : "");
  }
  function clearError() {
    error.hidden = true;
    for (const field of Array.from(fields)) {
      field.removeAttribute("aria-invalid");
      field.removeAttribute("aria-describedby");
    }
  }

  fields.password.addEventListener("input", validate);
  fields.confirmation.addEventListener("input", validate);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (saving) return;
    validate();
    if (!form.reportValidity()) return;
    clearError();
    saving = true;
    save.disabled = true;
    close.disabled = true;
    cancel.disabled = true;
    try {
      const result = await authRequest("password", {
        current_password: fields.current_password.value,
        password: fields.password.value,
      });
      form.reset();
      dialog.close();
      if (result.required && !result.username) location.replace(signInHref());
      else notify("Password updated.");
    } catch (issue) {
      showError(error, issue);
      const target = issue.field ? fields.namedItem(issue.field) : null;
      if (target) {
        target.setAttribute("aria-invalid", "true");
        target.setAttribute("aria-describedby", "auth-password-error");
        target.focus();
      }
    } finally {
      saving = false;
      save.disabled = false;
      close.disabled = false;
      cancel.disabled = false;
    }
  });
  dialog.addEventListener("cancel", event => { if (saving) event.preventDefault(); });
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (trigger.isConnected) trigger.focus();
  }, { once: true });
  dialog.append(top, error, form);
  document.body.append(dialog);
  dialog.showModal();
  fields.current_password.focus();
}

export async function setupAuthShell({ root = $("#auth-shell"), notify = () => {}, state } = {}) {
  if (!root) return;
  let session;
  try {
    session = state ?? await authRequest("state");
  } catch {
    root.hidden = true;
    return;
  }
  if (!session.required) {
    root.hidden = true;
    return;
  }
  if (!session.username) {
    location.replace(signInHref());
    return;
  }
  const menu = element("details", "auth-menu");
  const summary = element("summary", "secondary");
  summary.append(element("span", "auth-menu-name", session.username), icon("chevron-down"));
  summary.setAttribute("aria-label", "Account menu for " + session.username);
  const choices = element("div", "panel auth-menu-actions");
  const password = button("Change password", "text-button");
  password.addEventListener("click", () => {
    menu.open = false;
    passwordDialog(summary, notify);
  });
  const logout = button("Sign out", "text-button");
  const error = makeAlert();
  logout.addEventListener("click", async () => {
    logout.disabled = true;
    error.hidden = true;
    try {
      await authRequest("logout", {});
      location.replace("/auth");
    } catch (issue) {
      showError(error, issue);
      logout.disabled = false;
    }
  });
  menu.addEventListener("keydown", event => {
    if (event.key === "Escape" && menu.open) {
      menu.open = false;
      summary.focus();
    }
  });
  choices.append(password, logout, error);
  menu.append(summary, choices);
  root.replaceChildren(menu);
  root.hidden = false;
}
