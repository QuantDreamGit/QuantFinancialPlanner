import { $, ApiError, apiMessage } from "./ui.js";

export async function authRequest(action, data) {
  const response = await fetch("/api/auth/" + action, {
    method: data === undefined ? "GET" : "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: data === undefined ? { Accept: "application/json" }
      : { Accept: "application/json", "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new ApiError(payload.error?.message ?? "The account request could not be completed.",
      payload.error?.field, response.status);
  }
  return payload;
}

export function dashboardDestination(next, origin = location.origin) {
  if (!next) return "/";
  try {
    const url = new URL(next, origin);
    if (url.origin === origin && url.pathname === "/" && !url.username && !url.password) {
      return url.pathname + url.search + url.hash;
    }
  } catch {
    // A malformed return URL opens the dashboard.
  }
  return "/";
}

export function signInHref(here = location) {
  const next = dashboardDestination(here.pathname + here.search + here.hash, here.origin);
  return next === "/" ? "/auth" : "/auth?" + new URLSearchParams({ next });
}

export function authLocation(here = location) {
  const fragment = new URLSearchParams(here.hash.slice(1));
  const invite = fragment.get("invite");
  const bootstrap = fragment.get("setup");
  const next = new URLSearchParams(here.search).get("next") ?? "/";
  const target = dashboardDestination(next, here.origin);
  const inheritedFragment = !invite && !bootstrap && !target.includes("#") ? here.hash : "";
  return { invite, bootstrap, destination: target + inheritedFragment };
}

const modes = {
  login: {
    heading: "Sign in", action: "Sign in",
    description: "Open your Quant workspace.",
  },
  register: {
    heading: "Create an account", action: "Create account",
    description: "Choose a username and password for your personal workspace.",
  },
  setup: {
    heading: "Set up Quant", action: "Create administrator",
    description: "Find the setup code in the server terminal and create the first account.",
  },
  accept: {
    heading: "Accept an invitation", action: "Create account",
    description: "Enter the invitation code and choose your sign-in details.",
  },
};

function startAuthPage(root) {
  const navigation = authLocation();
  if (navigation.invite || navigation.bootstrap) {
    history.replaceState(history.state, "", location.pathname + location.search);
  }
  const form = $("#auth-form", root);
  const heading = $("#auth-heading", root);
  const description = $("#auth-description", root);
  const error = $("#auth-error", root);
  const options = $("#auth-options", root);
  const register = $("#auth-register", root);
  const alternate = $("#auth-alternate", root);
  const retry = $("#auth-retry", root);
  const submit = $("#auth-submit", root);
  const starterCategories = $("#auth-starter-categories", root);
  const fields = form.elements;
  fields.code.value = navigation.invite ?? "";
  fields.setup_code.value = navigation.bootstrap ?? "";
  let mode = "login";
  let busy = false;

  function toggleField(group, control, visible) {
    $(group, root).hidden = !visible;
    control.disabled = !visible;
    control.required = visible;
  }

  function clearError() {
    error.hidden = true;
    for (const field of Array.from(fields)) {
      field.removeAttribute("aria-invalid");
      field.removeAttribute("aria-describedby");
    }
  }

  function showError(issue) {
    clearError();
    error.hidden = false;
    error.textContent = apiMessage(issue);
    const target = issue.field ? fields.namedItem(issue.field) : null;
    if (target && !target.disabled) {
      target.setAttribute("aria-invalid", "true");
      target.setAttribute("aria-describedby", "auth-error");
      target.focus();
    }
  }

  function render(nextMode, restoreFocus = false) {
    mode = nextMode;
    const copy = modes[mode];
    clearError();
    heading.textContent = copy.heading;
    description.textContent = copy.description;
    document.title = copy.heading + " · Quant Financial Planner";
    submit.textContent = copy.action;
    fields.password.autocomplete = mode === "login" ? "current-password" : "new-password";
    fields.password.value = "";
    fields.confirmation.value = "";
    fields.confirmation.setCustomValidity("");
    toggleField("#auth-invitation", fields.code, mode === "accept");
    toggleField("#auth-setup-code", fields.setup_code, mode === "setup");
    toggleField("#auth-confirmation", fields.confirmation, mode !== "login");
    starterCategories.hidden = fields.starter_categories.disabled = mode === "login";
    alternate.textContent = mode === "login" ? "Accept an invitation" : "Back to sign in";
    register.hidden = mode !== "login";
    options.hidden = mode === "setup";
    retry.hidden = true;
    form.hidden = false;
    if (restoreFocus) (mode === "accept" && !fields.code.value ? fields.code : fields.username).focus();
  }

  function confirmPassword() {
    fields.confirmation.setCustomValidity(mode !== "login" && fields.confirmation.value !== fields.password.value
      ? "Enter the same password again." : "");
  }

  async function load() {
    retry.disabled = true;
    clearError();
    try {
      const state = await authRequest("state");
      if (!state.required || (state.username && !navigation.invite)) {
        location.replace(navigation.destination);
        return;
      }
      render(state.configured ? (navigation.invite ? "accept" : "login") : "setup");
    } catch (issue) {
      description.textContent = "Could not check account access.";
      showError(issue);
      retry.hidden = false;
    } finally {
      retry.disabled = false;
    }
  }

  alternate.addEventListener("click", () => {
    if (!busy) render(mode === "login" ? "accept" : "login", true);
  });
  register.addEventListener("click", () => {
    if (!busy) render("register", true);
  });
  retry.addEventListener("click", load);
  fields.password.addEventListener("input", confirmPassword);
  fields.confirmation.addEventListener("input", confirmPassword);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy) return;
    confirmPassword();
    if (!form.reportValidity()) return;
    clearError();
    busy = true;
    submit.disabled = true;
    alternate.disabled = true;
    register.disabled = true;
    try {
      const data = { username: fields.username.value.trim(), password: fields.password.value };
      if (mode === "accept") data.code = fields.code.value.trim();
      if (mode === "setup") data.setup_code = fields.setup_code.value.trim();
      if (mode !== "login") data.starter_categories = fields.starter_categories.checked;
      await authRequest(mode, data);
      form.reset();
      location.replace(mode === "register" ? "/" : navigation.destination);
    } catch (issue) {
      showError(issue);
    } finally {
      busy = false;
      submit.disabled = false;
      alternate.disabled = false;
      register.disabled = false;
    }
  });
  load();
}

if (typeof document !== "undefined") {
  const authView = $("#auth-view");
  if (authView) startAuthPage(authView);
}
