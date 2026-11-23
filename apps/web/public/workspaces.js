import { $, api, apiMessage, button, element, options } from "./ui.js";

export const workspaceId = () => new URLSearchParams(location.search).get("workspace")
  || document.documentElement.dataset.profileId || "1";

export function mayOpenProfile(id) {
  const profile = document.documentElement.dataset.profileId;
  const accessible = document.documentElement.dataset.openProfiles?.split(",") ?? [];
  return !id || !profile || String(id) === profile || accessible.includes(String(id));
}

export function workspaceHref(id, view = "overview") {
  const url = new URL(location.href);
  url.searchParams.set("workspace", id);
  url.hash = view;
  return url.href;
}

export function setupWorkspaces(root, { openDemo = () => {} } = {}) {
  const switcher = $("#workspace-switcher");
  let state;
  const managed = () => state?.managed ?? [];
  const heading = element("div", "page-heading");
  const title = element("div");
  title.append(element("h1", "", "Workspaces"),
    element("p", "page-description", "Personal profiles keep separate ledgers. Groups bring their member balances into one view."));
  heading.append(title);
  const list = element("div", "workspace-cards");
  const formPanel = element("section", "panel workspace-card");
  const form = element("form", "workspace-form");
  const formTitle = element("h2", "", "Create a workspace");
  const name = element("input");
  name.required = true;
  name.maxLength = 65;
  name.placeholder = "e.g. Family";
  const nameLabel = element("label", "field");
  nameLabel.append(element("span", "", "Name"), name);
  const kind = element("select");
  options(kind, [["profile", "Personal profile"], ["group", "Group"]], "profile");
  const kindLabel = element("label", "field");
  kindLabel.append(element("span", "", "Type"), kind);
  const selected = element("fieldset", "workspace-members");
  const starter = element("input");
  starter.type = "checkbox";
  starter.name = "starter_categories";
  const starterLabel = element("label", "workspace-check");
  const starterCopy = element("span", "", "Add starter categories ");
  starterCopy.append(element("small", "muted", "such as Employment, Groceries, and Index funds"));
  starterLabel.append(starter, starterCopy);
  const notice = element("div");
  notice.setAttribute("role", "status");
  const submit = button("Create workspace", "primary");
  submit.type = "submit";
  const cancel = button("Cancel edit", "text-button");
  cancel.hidden = true;
  const actions = element("div", "new-view-actions");
  actions.append(submit, cancel);
  form.append(nameLabel, kindLabel, selected, starterLabel, actions, notice);
  formPanel.append(formTitle, form);
  const content = element("div", "workspace-layout");
  content.append(list, formPanel);
  const demoPanel = element("section", "panel workspace-card workspace-demo");
  const demoCopy = element("div");
  demoCopy.append(element("h2", "", "Sample workspaces"),
    element("p", "muted", "Explore an editable personal ledger and family group with example history, goals, and payments."));
  const demoActions = element("div", "new-view-actions");
  const openSample = button("Open sample workspace");
  const openFamily = button("Open Sample Family");
  const demoError = element("p", "notice");
  demoError.hidden = true;
  demoError.setAttribute("role", "status");
  demoActions.append(openSample, openFamily);
  demoPanel.append(demoCopy, demoActions, demoError);
  root.append(heading, demoPanel, content);
  async function showDemo(role) {
    openSample.disabled = openFamily.disabled = true;
    demoError.hidden = true;
    try {
      await openDemo("overview", role);
    } catch (error) {
      demoError.textContent = apiMessage(error);
      demoError.hidden = false;
    } finally {
      openSample.disabled = openFamily.disabled = false;
    }
  }
  openSample.addEventListener("click", () => showDemo("personal"));
  openFamily.addEventListener("click", () => showDemo("family"));
  let editing = null;

  function memberChoices(active = []) {
    selected.hidden = kind.value !== "group";
    selected.replaceChildren(element("legend", "", "Member profiles"));
    const profiles = managed().filter(item => item.kind === "profile");
    for (const profile of profiles) {
      const field = element("label", "workspace-check");
      const input = element("input");
      input.type = "checkbox";
      input.value = profile.id;
      input.checked = active.some(member => member.id === profile.id);
      field.append(input, profile.name);
      selected.append(field);
    }
    if (profiles.length < 2) {
      selected.append(element("p", "muted", "Create a second personal profile to make a group."));
    } else {
      selected.append(element("p", "muted", "Members can see the balances and activity shared by every profile in this group."));
    }
  }

  function edit(workspace) {
    editing = workspace;
    formTitle.textContent = workspace ? "Edit " + workspace.name : "Create a workspace";
    name.value = workspace?.name ?? "";
    kind.value = workspace?.kind ?? "profile";
    kind.disabled = Boolean(workspace);
    starterLabel.hidden = starter.disabled = Boolean(workspace);
    starter.checked = false;
    submit.textContent = workspace ? "Save changes" : "Create workspace";
    cancel.hidden = !workspace;
    notice.replaceChildren();
    memberChoices(workspace?.members);
    if (workspace) name.focus();
  }

  function render() {
    const editable = managed();
    const visible = new Set(state.workspaces.map(item => item.id));
    formPanel.hidden = !editable.length;
    content.classList.toggle("workspace-layout-readonly", !editable.length);
    const cards = editable.length ? [...editable, ...state.workspaces.filter(item => item.demo_role)] : state.workspaces;
    list.replaceChildren(...cards.map(workspace => {
      const card = element("article", "panel workspace-card");
      const title = element("h2", "", workspace.name);
      const copy = workspace.kind === "group" ? workspace.members.map(member => member.name).join(" · ")
        : workspace.groups.length ? "Member of " + workspace.groups.map(group => group.name).join(", ") : "Personal ledger";
      card.append(title, element("p", "muted", copy));
      const actions = element("div", "new-view-actions");
      if (workspace.demo_role) actions.append(element("span", "pill", "Sample data"));
      if (workspace.id === state.active.id) {
        actions.append(element("span", "pill mint", "Current workspace"));
      } else if (visible.has(workspace.id)) {
        const open = element("a", "secondary", "Open workspace");
        open.href = workspaceHref(workspace.id);
        actions.append(open);
      }
      if (editable.length && !workspace.demo_role) {
        const configure = button("Edit", "text-button", "edit");
        configure.addEventListener("click", () => edit(workspace));
        actions.append(configure);
      }
      card.append(actions);
      return card;
    }));
  }

  async function load() {
    try {
      state = await api("/api/workspaces");
      options(switcher, state.workspaces.map(item => [item.id, item.name + (item.kind === "group" ? " (group)" : "")]), state.active.id);
      switcher.disabled = false;
      document.documentElement.dataset.workspaceKind = state.active.kind;
      document.documentElement.dataset.demoRole = state.active.demo_role ?? "";
      document.documentElement.dataset.openProfiles = state.workspaces.filter(item => item.kind === "profile")
        .map(item => item.id).join(",");
      render();
      if (!editing) memberChoices();
      return state.active;
    } catch (error) {
      if ([403, 404].includes(error.status) && new URLSearchParams(location.search).has("workspace")) {
        location.replace("/" + location.hash);
        return null;
      }
      notice.replaceChildren(element("p", "notice", apiMessage(error)));
      return null;
    }
  }

  switcher.addEventListener("change", () => {
    const view = location.hash === "#cars" ? "cars" : "overview";
    location.assign(workspaceHref(switcher.value, view));
  });
  kind.addEventListener("change", () => memberChoices());
  cancel.addEventListener("click", () => edit(null));
  form.addEventListener("submit", async event => {
    event.preventDefault();
    submit.disabled = true;
    const old = editing;
    try {
      const data = { name: name.value, kind: kind.value, members: kind.value === "group"
        ? [...selected.querySelectorAll("input:checked")].map(input => Number(input.value)) : [] };
      if (!old) data.starter_categories = starter.checked;
      const result = await api("/api/workspaces" + (old ? "/" + old.id : ""), { method: old ? "PUT" : "POST", data });
      edit(null);
      await load();
      notice.replaceChildren(element("p", "muted", old ? "Workspace updated." : result.name + " created."));
    } catch (error) {
      notice.replaceChildren(element("p", "notice", apiMessage(error)));
    } finally {
      submit.disabled = false;
    }
  });
  return { load };
}
