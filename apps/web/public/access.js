import { api, apiMessage, button, element, options } from "./ui.js";

const base = "/api/auth/";
const inviteStatus = {
  pending: "Pending", used: "Accepted", revoked: "Revoked", expired: "Expired",
};
const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium", timeStyle: "short",
});

function when(epoch) {
  return Number.isFinite(epoch) ? dateTime.format(new Date(epoch * 1000)) : "—";
}

function panel(title, caption) {
  const box = element("section", "panel access-panel");
  const header = element("div", "panel-heading", element("div", "", element("h2", "", title)));
  if (caption) header.firstElementChild.append(element("p", "", caption));
  box.append(header);
  return box;
}

function field(label, control) {
  const wrapper = element("label", "field");
  wrapper.append(element("span", "", label), control);
  return wrapper;
}

function action(label, kind, callback) {
  const control = button(label, kind);
  control.addEventListener("click", callback);
  return control;
}

function statusChip(label, status) {
  const chip = element("span", "access-chip", label);
  chip.dataset.status = status;
  return chip;
}

function workspacesLink(label) {
  const link = element("a", "text-link", label);
  link.href = "#workspaces";
  return link;
}

function credentialLink(token) {
  const url = new URL("/auth", location.origin);
  url.hash = new URLSearchParams({ invite: token }).toString();
  return url.href;
}

export function setupAccess(root, { notify = () => {} } = {}) {
  let session, members, currentLink, request, generation = 0, busy = false, commit;
  const content = element("div", "access-content");
  const heading = element("h1", "", "Family access");
  const title = element("div", "page-heading", heading);
  const refresh = action("Refresh", "secondary", () => { if (!busy) load(); });
  const status = element("p", "notice");
  status.role = "alert";
  status.hidden = true;
  const confirm = element("dialog", "editor");
  const confirmTitle = element("h2");
  confirmTitle.id = "access-confirm-title";
  const confirmDetail = element("p", "muted access-confirm-detail");
  const confirmError = element("p", "notice");
  confirmError.role = "alert";
  confirmError.hidden = true;
  confirm.setAttribute("aria-labelledby", confirmTitle.id);
  const confirmForm = element("form");
  const confirmControls = element("div", "dialog-actions");
  const cancel = action("Cancel", "secondary", () => confirm.close());
  const apply = element("button", "danger-button");
  apply.type = "submit";
  confirmControls.append(cancel, apply);
  confirmForm.append(confirmTitle, confirmDetail, confirmError, confirmControls);
  confirm.append(confirmForm);

  function eraseLink() {
    currentLink = null;
    for (const control of root.querySelectorAll("[data-private-invite]")) control.value = "";
  }

  function hide() {
    generation++;
    request?.abort();
    request = null;
    session = members = commit = null;
    eraseLink();
    if (confirm.open) confirm.close();
    confirmError.hidden = true;
    status.hidden = true;
    status.textContent = "";
    busy = false;
    apply.disabled = cancel.disabled = false;
    refresh.remove();
    content.replaceChildren();
    root.replaceChildren();
  }

  function setError(message = "") {
    status.textContent = message;
    status.hidden = !message;
  }

  async function open(authState) {
    hide();
    session = authState;
    root.replaceChildren(title, status, content, confirm);
    if (!session?.required) {
      content.append(element("p", "access-message", "Family access is available when the server requires sign-in."));
      return;
    }
    if (!session.configured || !session.is_admin) {
      content.append(element("p", "access-message", "A family administrator can manage sign-in accounts and invitations."));
      return;
    }
    title.append(refresh);
    content.append(element("p", "muted", "Loading family access…"));
    await load();
  }

  async function load() {
    const epoch = generation;
    request?.abort();
    const controller = new AbortController();
    request = controller;
    try {
      const dashboard = await api(base + "members", { signal: controller.signal });
      if (controller.signal.aborted || epoch !== generation) return;
      members = dashboard;
      setError();
      render();
    } catch (error) {
      if (controller.signal.aborted || epoch !== generation) return;
      if (error.status === 401 || error.status === 403) {
        members = null;
        eraseLink();
        refresh.remove();
        content.replaceChildren(element("p", "access-message", apiMessage(error)));
      } else {
        setError(apiMessage(error));
        if (!members) content.replaceChildren(action("Retry", "secondary", load));
      }
    }
  }

  function ask(titleText, detailText, label, method, path, data, success) {
    if (busy) return;
    confirmTitle.textContent = titleText;
    confirmDetail.textContent = detailText;
    confirmError.hidden = true;
    apply.textContent = label;
    apply.className = method === "PUT" && data?.enabled ? "primary" : "danger-button";
    commit = { method, path, data, success };
    confirm.showModal();
    cancel.focus();
  }

  confirm.addEventListener("cancel", event => { if (busy) event.preventDefault(); });
  confirmForm.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !commit) return;
    const epoch = generation;
    const { method, path, data, success } = commit;
    busy = apply.disabled = cancel.disabled = true;
    confirmError.hidden = true;
    try {
      await api(base + path, { method, data });
      if (epoch !== generation) return;
      confirm.close();
      commit = null;
      notify(success);
      await load();
    } catch (error) {
      if (epoch !== generation) return;
      confirmError.textContent = apiMessage(error);
      confirmError.hidden = false;
    } finally {
      if (epoch === generation) {
        busy = apply.disabled = cancel.disabled = false;
        if (!confirm.open && members) render();
      }
    }
  });

  function invitePanel(profileById) {
    const section = panel("Invite a family member", "Choose a personal profile. The invitation is valid for one sign-in setup.");
    const claimed = new Set(members.users.map(user => user.profile_id));
    const reserved = new Set(members.invitations.filter(invite => invite.status === "pending").map(invite => invite.profile_id));
    const available = members.profiles.filter(profile => profile.kind === "profile" && !claimed.has(profile.id) && !reserved.has(profile.id));
    const form = element("form", "access-invite-form");
    const profile = element("select");
    profile.required = true;
    options(profile, available.map(item => [item.id, item.name]), "", "Choose a personal profile");
    const submit = element("button", "primary", "Create invitation");
    submit.type = "submit";
    profile.disabled = submit.disabled = busy || !available.length;
    form.append(field("Personal profile", profile), submit);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (busy || !profile.value) return;
      const epoch = generation;
      const id = Number(profile.value);
      busy = profile.disabled = submit.disabled = true;
      submit.textContent = "Creating…";
      setError();
      let issued = false;
      try {
        const invitation = await api(base + "invitations", { method: "POST", data: { profile_id: id } });
        if (epoch !== generation) return;
        eraseLink();
        currentLink = { id: invitation.id, url: credentialLink(invitation.token), expiry: invitation.expires_at,
          profile: profileById.get(id)?.name };
        members = { ...members, invitations: [
          { id: invitation.id, profile_id: id, expires_at: invitation.expires_at, status: "pending" },
          ...members.invitations.filter(item => item.profile_id !== id || item.status !== "pending"),
        ] };
        issued = true;
        render();
        notify("Invitation created.");
        await load();
      } catch (error) {
        if (epoch === generation) setError(apiMessage(error));
      } finally {
        if (epoch === generation) {
          busy = false;
          if (members) render();
          if (issued) root.querySelector("[data-private-invite]")?.focus();
        }
      }
    });
    section.append(form);
    const hint = element("p", "muted access-invite-hint");
    if (available.length) hint.append("Group access follows the profile’s memberships. ", workspacesLink("Manage workspaces"));
    else hint.append("All personal profiles have sign-in accounts or pending invitations. ", workspacesLink("Create a profile in Workspaces"));
    section.append(hint);

    if (currentLink) {
      const linkBox = element("div", "access-invite-link");
      const description = currentLink.profile ? "Invitation for " + currentLink.profile : "Invitation link";
      const link = element("input");
      link.type = "text";
      link.value = currentLink.url;
      link.readOnly = true;
      link.autocomplete = "off";
      link.spellcheck = false;
      link.dataset.privateInvite = "";
      link.addEventListener("focus", () => link.select());
      const feedback = element("p", "access-link-feedback");
      feedback.role = "status";
      const copy = action("Copy link", "secondary", async () => {
        try {
          await navigator.clipboard.writeText(link.value);
          feedback.textContent = "Link copied.";
        } catch {
          link.focus();
          link.select();
          feedback.textContent = "Link selected. Copy it with your browser or keyboard.";
        }
      });
      const dismiss = action("Hide link", "text-button", () => { eraseLink(); render(); });
      const values = element("div", "access-link-controls");
      values.append(field(description, link), copy, dismiss);
      linkBox.append(values, element("p", "muted access-invite-hint",
        "Share this link privately. It is shown here until you leave this page. Expires " + when(currentLink.expiry) + "."), feedback);
      section.append(linkBox);
    }
    return section;
  }

  function usersPanel(profileById) {
    const section = panel("Sign-in accounts");
    if (!members.users.length) {
      section.append(element("p", "muted access-message", "No sign-in accounts are registered."));
      return section;
    }
    const items = element("div", "access-list");
    for (const user of members.users) {
      const row = element("article", "access-row");
      const identity = element("div", "access-identity");
      const self = user.username === session.username;
      const role = user.is_admin ? "Administrator" : "Member";
      const label = profileById.get(user.profile_id)?.name ?? "Personal profile";
      identity.append(element("h3", "", user.username), element("p", "muted", label + " · " + role + (self ? " · Your account" : "")));
      const controls = element("div", "access-actions");
      controls.append(statusChip(user.enabled ? "Active" : "Disabled", user.enabled ? "active" : "disabled"));
      if (!self) {
        const sessions = action("Sign out sessions", "secondary", () => ask(
          "Sign out " + user.username + "?", "This ends their current sessions. They can sign in again.",
          "Sign out sessions", "DELETE", "members/" + user.id + "/sessions", undefined, "Sessions ended.",
        ));
        sessions.setAttribute("aria-label", "Sign out sessions for " + user.username);
        if (user.enabled) controls.append(sessions);
        const toggle = action(user.enabled ? "Disable access" : "Enable access", user.enabled ? "danger-button" : "secondary", () => ask(
          (user.enabled ? "Disable access for " : "Enable access for ") + user.username + "?",
          user.enabled ? "This signs them out and blocks new sign-ins. Their ledger and group relationships are kept."
            : "This lets the member sign in again using their existing account.",
          user.enabled ? "Disable access" : "Enable access", "PUT", "members/" + user.id, { enabled: !user.enabled },
          user.enabled ? "Access disabled." : "Access enabled.",
        ));
        toggle.setAttribute("aria-label", (user.enabled ? "Disable access for " : "Enable access for ") + user.username);
        controls.append(toggle);
      }
      for (const control of controls.querySelectorAll("button")) control.disabled = busy;
      row.append(identity, controls);
      items.append(row);
    }
    section.append(items);
    return section;
  }

  function invitationsPanel(profileById) {
    const section = panel("Invitations");
    if (!members.invitations.length) {
      section.append(element("p", "muted access-message", "Created invitations will appear here."));
      return section;
    }
    const items = element("div", "access-list");
    for (const invite of members.invitations) {
      const row = element("article", "access-row");
      const identity = element("div", "access-identity");
      const profile = profileById.get(invite.profile_id)?.name ?? "Personal profile";
      const detail = invite.status === "used" ? "Accepted " + when(invite.used_at)
        : invite.status === "revoked" ? "Revoked " + when(invite.revoked_at)
          : (invite.status === "expired" ? "Expired " : "Expires ") + when(invite.expires_at);
      identity.append(element("h3", "", profile), element("p", "muted",
        Number.isFinite(invite.created_at) ? "Created " + when(invite.created_at) + " · " + detail : detail));
      const controls = element("div", "access-actions");
      controls.append(statusChip(inviteStatus[invite.status] ?? invite.status, invite.status));
      if (invite.status === "pending") {
        const revoke = action("Revoke invitation", "secondary", () => ask(
          "Revoke the invitation for " + profile + "?", "This link will stop working immediately.",
          "Revoke invitation", "DELETE", "invitations/" + invite.id, undefined, "Invitation revoked.",
        ));
        revoke.setAttribute("aria-label", "Revoke invitation for " + profile);
        revoke.disabled = busy;
        controls.append(revoke);
      }
      row.append(identity, controls);
      items.append(row);
    }
    section.append(items);
    return section;
  }

  function render() {
    if (!members) return;
    const knownLink = currentLink && members.invitations.find(invite => invite.id === currentLink.id);
    if (knownLink && knownLink.status !== "pending") eraseLink();
    const profileById = new Map(members.profiles.map(profile => [profile.id, profile]));
    content.replaceChildren(invitePanel(profileById), usersPanel(profileById), invitationsPanel(profileById));
  }

  return { open, hide };
}
