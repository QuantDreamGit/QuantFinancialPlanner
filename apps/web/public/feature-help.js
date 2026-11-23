import { topicGroups } from "./documentation-topics.js";
import { $, $$, button, element, icon } from "./ui.js";

const topics = new Map(topicGroups.flatMap(group => group.topics.map(topic => [topic.id, topic])));
const pageTopics = {
  overview: "overview-and-trends",
  flow: "flow-and-replay",
  replay: "flow-and-replay",
  insights: "insights",
  transactions: "accounts-and-transactions",
  imports: "statement-imports",
  shared: "shared-payments",
  accounts: "accounts-and-transactions",
  categories: "categories-and-labels",
  goals: "goals-and-savings",
  returns: "monthly-returns",
  forecast: "cash-runway",
  funding: "funding-check",
  decision: "decision-lab",
  planner: "purchase-planner",
  budgets: "family-limits",
  workspaces: "family-workspaces",
  access: "family-workspaces",
  documentation: "getting-started",
};

const views = {
  transactions: {
    name: "Transactions", topic: "accounts-and-transactions",
    copy: "Add and review posted activity by date, account, and category. Use the paperclip to manage documents for a transaction.",
  },
  accounts: {
    name: "Accounts", topic: "accounts-and-transactions",
    copy: "Track balances for checking, cash, savings, and investments. Set the opening date and balance for each account.",
  },
  workspaces: {
    name: "Workspaces", topic: "family-workspaces",
    copy: "Switch between personal ledgers, shared groups, and private practice workspaces.",
  },
  access: {
    name: "Family access", topic: "family-workspaces",
    copy: "Invite family members, manage their sign-ins, and revoke sessions or unused invitations.",
  },
  replay: {
    name: "Money flow replay", topic: "flow-and-replay",
    copy: "Compare the same income, expense, or investment branches across two months.",
  },
  documentation: {
    name: "Documentation", topic: "getting-started",
    copy: "Search the handbook for a feature, or open the quick-start guide to visit the main views in order.",
  },
};

const contexts = {
  "forecast:runway": {
    name: "Cash runway", topic: "cash-runway",
    copy: "Inspect projected cash day by day. Open a date to review its bills, income, returns, and reimbursements.",
  },
  "forecast:simulator": {
    name: "What-if simulator", topic: "what-if",
    copy: "Shift a planned purchase or change recurring amounts to compare cash balances and reserve breaches.",
  },
  "forecast:actual": {
    name: "Plan vs. actual", topic: "plan-vs-actual",
    copy: "Compare a saved plan with month-end ledger cash. Saved category estimates help explain where the plan and actual results diverged.",
  },
  "decision:risk": {
    name: "Risk simulator", topic: "decision-lab",
    copy: "Run possible cash paths with the income, bills, and reimbursement delays you set. Inspect the projected range and reserve success rate.",
  },
  "decision:reverse": {
    name: "Reverse planner", topic: "reverse-planner",
    copy: "See the maximum modeled purchase amount by date at the selected reserve and success threshold.",
  },
  "decision:optimize": {
    name: "Decision optimizer", topic: "decision-optimizer",
    copy: "Compare purchase dates, investment contributions, and goal allocations on the selected grid. Review each feasible option before applying changes in your ledger.",
  },
  "decision:tradeoff": {
    name: "Goal trade-off", topic: "goal-tradeoff",
    copy: "See how redirecting monthly savings on a shared funding account changes the completion dates of two goals.",
  },
  "decision:score": {
    name: "Forecast scorecard", topic: "forecast-scorecard",
    copy: "Compare saved forecasts against elapsed dates in the ledger using error size, direction, and risk-band coverage.",
  },
};

function descriptor(view, context, heading) {
  const detail = contexts[view + ":" + context] ?? views[view];
  if (detail) return detail;
  const slug = pageTopics[view];
  return slug ? { name: heading.textContent.trim(), topic: slug, copy: topics.get(slug).summary } : null;
}

export function setupFeatureHelp({ root = $("#main") } = {}) {
  const mounted = new WeakMap();
  let activeView;
  let activeSection;
  let expanded;
  let sequence = 0;

  function close(restore = false) {
    if (!expanded) return;
    expanded.panel.hidden = true;
    expanded.trigger.setAttribute("aria-expanded", "false");
    if (restore && expanded.trigger.isConnected) expanded.trigger.focus();
    expanded = null;
  }

  const observer = new MutationObserver(scan);

  function attach(heading, detail) {
    if (!heading || !detail) return;
    let widget = mounted.get(heading);
    if (!widget || !widget.wrap.contains(heading)) {
      const wrap = element("div", "feature-help-heading");
      const trigger = button("i", "icon-link feature-help-trigger");
      const panel = element("div", "panel feature-help-popover");
      panel.id = "feature-help-" + ++sequence;
      panel.hidden = true;
      panel.setAttribute("role", "region");
      trigger.setAttribute("aria-controls", panel.id);
      trigger.setAttribute("aria-expanded", "false");
      heading.before(wrap);
      wrap.append(heading, trigger, panel);
      widget = { wrap, trigger, panel, detail: null };
      mounted.set(heading, widget);
      trigger.addEventListener("click", () => {
        const closing = expanded === widget;
        close();
        if (!closing) {
          widget.panel.hidden = false;
          widget.trigger.setAttribute("aria-expanded", "true");
          expanded = widget;
        }
      });
    }
    if (widget.detail?.name === detail.name && widget.detail?.topic === detail.topic) return;
    if (expanded === widget) close();
    widget.detail = detail;
    widget.trigger.setAttribute("aria-label", "About " + detail.name);
    widget.panel.setAttribute("aria-label", "About " + detail.name);
    const topic = topics.get(detail.topic);
    const match = element("a", "text-link", "Read in the handbook");
    match.href = "#documentation/" + topic.id;
    match.setAttribute("aria-label", "Read “" + topic.title + "” in the handbook");
    match.append(icon("arrow-right"));
    match.addEventListener("click", () => close());
    widget.panel.replaceChildren(element("strong", "", detail.name), element("p", "", detail.copy), match);
  }

  function scan() {
    if (!activeSection) return;
    const heading = $("h1", activeSection);
    if (heading) attach(heading, descriptor(activeView, activeSection.dataset.helpContext, heading));
    for (const subheading of $$("[data-feature-help]", activeSection)) {
      attach(subheading, contexts[activeView + ":" + subheading.dataset.featureHelp]);
    }
    if (expanded && !expanded.wrap.isConnected) close();
  }

  function show(view) {
    close();
    observer.disconnect();
    activeView = view;
    activeSection = $$("[data-view]", root).find(section => section.dataset.view === view);
    if (!activeSection) return;
    scan();
    observer.observe(activeSection, {
      childList: true, subtree: true, attributes: true, attributeFilter: ["data-help-context", "data-feature-help"],
    });
  }

  document.addEventListener("click", event => {
    if (expanded && !expanded.wrap.contains(event.target)) close();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !event.defaultPrevented && expanded) {
      event.preventDefault();
      close(true);
    }
  });
  return { show };
}
