import { topicGroups } from "./documentation-topics.js";
import { apiMessage, button, element, icon } from "./ui.js";

const home = "getting-started";
const topics = new Map(topicGroups.flatMap(group => group.topics.map(topic => [topic.id, topic])));
const storagePrefix = "quant-first-run-guide-v1-";
const guideSteps = [
  {
    view: "overview", group: "overview", name: "Overview", path: "Overview",
    title: "Read your overview", topic: "overview-and-trends", samples: true,
    description: "Find balances and period totals, then change the chart to compare wealth or cash flow. Private sample workspaces have editable fictional data for this walkthrough.",
  },
  {
    view: "accounts", group: "manage", name: "Accounts", path: "Manage → Accounts",
    title: "Set your starting balances", topic: "accounts-and-transactions",
    description: "Accounts connect the ledger to the balances you track. Choose an account type, opening date, and balance. Use the opening date that matches the balance you enter.",
  },
  {
    view: "categories", group: "manage", name: "Categories", path: "Manage → Categories",
    title: "Build your category tree", topic: "categories-and-labels",
    description: "Use Add starter categories for preset income, expense, and investment groups. Add your own child categories to make totals easier to explore in Money flow.",
  },
  {
    view: "transactions", group: "activity", name: "Transactions", path: "Activity → Transactions",
    title: "Review your ledger", topic: "accounts-and-transactions",
    description: "Search or filter posted activity by period, account, and category. Add an entry when money actually moves. Transfers connect two tracked accounts.",
  },
  {
    view: "imports", group: "activity", name: "Import payments", path: "Activity → Import payments",
    title: "Review a statement", topic: "statement-imports",
    description: "Choose a statement and ledger account. Preview and review each row, then reconcile it with an existing entry, record it, or skip it.",
  },
  {
    view: "flow", group: "overview", name: "Money flow", path: "Overview → Money flow",
    title: "Follow the money", topic: "flow-and-replay",
    description: "Open a flow and expand a category to see the amounts and entries behind it. Use the replay to compare how the same branches changed between months.",
  },
  {
    view: "insights", group: "overview", name: "Insights", path: "Overview → Insights",
    title: "Investigate what changed", topic: "insights",
    description: "Select a month and open a waterfall step to see the entries behind it. Spending detective links recurring charges and unusual category totals to the history that flagged them.",
  },
  {
    view: "shared", group: "activity", name: "Shared payments", path: "Activity → Shared payments",
    title: "Split an expense", topic: "shared-payments",
    description: "Choose who paid and how much each person owes. Check the cash paid and your personal expense separately, then record reimbursements when they arrive.",
  },
  {
    view: "forecast", group: "planning", name: "Forecast", path: "Planning → Forecast",
    title: "Look ahead to the next payment", topic: "cash-runway",
    description: "Cash runway puts future paydays and bills on a calendar. Select a day to see what changes its balance, then post or match a commitment when it happens.",
  },
  {
    view: "funding", group: "planning", name: "Funding check", path: "Planning → Funding check",
    title: "Check if a payment is covered", topic: "funding-check",
    description: "Choose the payment account and date. Review any first shortfall, which approved transfers arrive in time, and what changes when reimbursements are delayed.",
  },
  {
    view: "planner", group: "planning", name: "Purchase planner", path: "Planning → Purchase planner",
    title: "Compare ways to pay for a purchase", topic: "purchase-planner",
    description: "Compare upfront and gradual funding using a planned amount, date, and reserve. Save the original projection to compare it with ledger balances later.",
  },
  {
    view: "goals", group: "planning", name: "Goals", path: "Planning → Goals",
    title: "Connect savings to a goal", topic: "goals-and-savings",
    description: "See what is earmarked and how much it covers. Set a deadline to estimate a monthly contribution and compare it with other goals on the same accounts.",
  },
  {
    view: "returns", group: "planning", name: "Monthly returns", path: "Planning → Monthly returns",
    title: "Schedule interest and coupon income", topic: "monthly-returns",
    description: "Choose an instrument, a fixed amount or rate on principal, and a payout destination. Review and record returns as you receive them.",
  },
  {
    view: "decision", group: "planning", name: "Decision lab", path: "Planning → Decision lab",
    title: "Compare possible decisions", topic: "decision-lab",
    description: "Explore how uncertainty affects cash reserves, compare purchase and investment choices, and use the scorecard to see how saved forecasts held up.",
  },
  {
    view: "budgets", group: "planning", name: "Group limits", path: "Planning → Group limits",
    title: "Track a shared spending limit", topic: "family-limits",
    description: "Compare actual and projected group spending for the household, a member, or a category. Switch to Sample Family to practice with fictional group records.",
  },
  {
    view: "workspaces", group: "manage", name: "Workspaces", path: "Manage → Workspaces",
    title: "Choose the ledger you need", topic: "family-workspaces",
    description: "Switch between private personal ledgers and shared groups. Reopen the personal or family sample from Workspaces to practice with a separate fictional ledger.",
  },
  {
    title: "Continue in your workspace",
    description: "Open the handbook to find examples and instructions for each feature. The info button beside each view heading links to the relevant topic.",
    topic: "getting-started",
  },
];

function link(label, href, className) {
  const node = element("a", className, label);
  node.href = href;
  return node;
}

function topicLink(topic, className) {
  return link(topic.title, "#documentation/" + topic.id, className);
}

function searchText(value) {
  return value.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

const topicText = new Map(topicGroups.flatMap(group => group.topics.map(topic => [
  topic.id,
  searchText([group.title, topic.id, topic.title, topic.summary, topic.keywords ?? "", topic.example ?? "",
    ...topic.sections.flatMap(section => [section.title, ...(section.copy ?? []),
      ...(section.steps ?? []), ...(section.bullets ?? [])])].join(" ")),
])));

function list(items, tag) {
  const node = element(tag);
  node.append(...items.map(item => element("li", "", item)));
  return node;
}

function createTour(openDemo, navigateTo) {
  const dock = element("aside", "panel documentation-guide");
  dock.hidden = true;
  dock.setAttribute("aria-labelledby", "documentation-guide-title");
  const progress = element("progress");
  progress.max = guideSteps.length;
  progress.setAttribute("aria-label", "Guide progress");
  const stepLabel = element("p", "documentation-guide-step");
  const title = element("h2");
  title.id = "documentation-guide-title";
  title.tabIndex = -1;
  const description = element("p", "documentation-guide-copy");
  description.id = "documentation-guide-description";
  description.setAttribute("aria-live", "polite");
  const handbook = link("Read this topic", "#documentation", "text-link documentation-guide-topic");
  const route = element("div", "documentation-guide-route");
  const path = element("strong");
  const go = button("", "secondary", "arrow-right");
  route.append(path, go);
  const sampleButtons = element("div", "documentation-guide-samples");
  const personal = button("Open sample workspace", "secondary", "wallet");
  const family = button("Open Sample Family", "secondary", "workspace");
  sampleButtons.append(personal, family);
  const errorMessage = element("p", "notice documentation-error");
  errorMessage.setAttribute("role", "alert");
  errorMessage.hidden = true;
  const skip = button("Skip guide", "text-button");
  const skipStep = button("Skip step", "text-button");
  const back = button("Back", "secondary");
  const next = button("Next", "primary");
  const navigation = element("div", "documentation-guide-navigation");
  const actions = element("div", "documentation-guide-buttons");
  actions.append(back, skipStep, next);
  navigation.append(skip, actions);
  dock.append(stepLabel, progress, title, route, description, handbook, sampleButtons, errorMessage, navigation);
  document.body.append(dock);

  const memory = new Map();
  let profile = null;
  let step = 0;
  let busy = false;
  let currentView = "";
  let target = null;
  let returnFocus;
  const visited = new Set();

  function marker(key) {
    if (memory.has(key)) return memory.get(key);
    try {
      return localStorage.getItem(key) ?? null;
    } catch {
      return null;
    }
  }

  function setMarker(key, value) {
    if (!key) return;
    memory.set(key, value);
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch {
      // Keep this session’s preference if browser storage is unavailable.
    }
  }

  function highlight(current, waiting) {
    const targetLink = waiting ? document.querySelector('[data-nav="' + current.view + '"]:not([hidden])')
      ?? document.querySelector('[data-nav-group="' + current.group + '"]:not([hidden])') : null;
    if (targetLink === target) return;
    target?.removeAttribute("data-guide-target");
    target = targetLink;
    if (target) {
      target.setAttribute("data-guide-target", "");
      target.scrollIntoView?.({ block: "nearest", inline: "nearest" });
    }
  }

  function renderStep({ focus = false } = {}) {
    const current = guideSteps[step];
    const lastStep = step === guideSteps.length - 1;
    if (current.view && currentView === current.view) visited.add(step);
    const waiting = Boolean(current.view && !visited.has(step));
    stepLabel.textContent = "Quick start · Step " + (step + 1) + " of " + guideSteps.length;
    progress.value = step + 1;
    title.textContent = current.title;
    path.textContent = current.path ?? "";
    go.textContent = "Go to " + (current.name ?? "");
    route.hidden = !waiting;
    handbook.href = "#documentation/" + current.topic;
    handbook.hidden = waiting;
    description.textContent = waiting ? "Open the highlighted navigation item to reach " + current.name + "." : current.description;
    back.hidden = step === 0;
    next.textContent = lastStep ? "Finish guide" : "Next";
    next.disabled = waiting || busy;
    skipStep.hidden = !waiting;
    sampleButtons.hidden = !current.samples || typeof openDemo !== "function";
    errorMessage.hidden = true;
    highlight(current, waiting);
    if (focus) title.focus();
  }

  function begin(start, focus) {
    if (!dock.hidden) return;
    returnFocus = focus ? document.activeElement : null;
    visited.clear();
    step = start;
    setMarker(profile, "step:" + step);
    dock.hidden = false;
    renderStep({ focus });
  }

  function showGuide() {
    begin(0, true);
  }

  function maybeShowGuide(profileId) {
    if (profileId === null || profileId === undefined || profileId === "") return;
    profile = storagePrefix + String(profileId);
    const saved = marker(profile);
    if (!saved) begin(0, false);
    else {
      const match = /^step:(\d+)$/.exec(saved);
      if (match && Number(match[1]) < guideSteps.length) begin(Number(match[1]), false);
    }
  }

  function closeGuide(value) {
    setMarker(profile, value);
    highlight(guideSteps[step], false);
    dock.hidden = true;
    if (returnFocus?.isConnected && !returnFocus.closest("[hidden]")) returnFocus.focus();
  }

  skip.addEventListener("click", () => closeGuide("skipped"));
  dock.addEventListener("keydown", event => {
    if (event.key === "Escape" && !busy) {
      event.preventDefault();
      closeGuide("skipped");
    }
  });
  function advance() {
    if (step === guideSteps.length - 1) {
      closeGuide("completed");
      return;
    }
    move(1);
  }
  function move(direction) {
    step += direction;
    setMarker(profile, "step:" + step);
    renderStep({ focus: true });
  }
  go.addEventListener("click", () => navigateTo(guideSteps[step].view));
  back.addEventListener("click", () => move(-1));
  next.addEventListener("click", advance);
  skipStep.addEventListener("click", advance);

  async function enterSample(role) {
    if (busy) return;
    const key = profile;
    const previous = marker(key);
    busy = true;
    personal.disabled = family.disabled = back.disabled = next.disabled = skip.disabled = skipStep.disabled = go.disabled = true;
    errorMessage.hidden = true;
    setMarker(key, "step:" + step);
    try {
      await openDemo("overview", role);
    } catch (error) {
      setMarker(key, previous);
      errorMessage.textContent = apiMessage(error);
      errorMessage.hidden = false;
    } finally {
      busy = false;
      personal.disabled = family.disabled = back.disabled = skip.disabled = skipStep.disabled = go.disabled = false;
      next.disabled = Boolean(guideSteps[step].view && !visited.has(step));
    }
  }

  personal.addEventListener("click", () => enterSample("personal"));
  family.addEventListener("click", () => enterSample("family"));
  return {
    maybeShowGuide, showGuide,
    onNavigate(view) {
      currentView = view;
      if (!dock.hidden) renderStep();
    },
  };
}

export function setupDocumentation(root, { openDemo, notify = () => {}, navigateTo = view => { location.hash = view; } }) {
  const guide = createTour(openDemo, navigateTo);
  const pageHeading = element("div", "page-heading");
  const headingCopy = element("div");
  headingCopy.append(element("h1", "", "Documentation"),
    element("p", "page-description", "Find instructions and examples for managing, understanding, and planning your finances."));
  const showGuide = button("Open quick-start guide", "secondary", "documentation");
  showGuide.addEventListener("click", guide.showGuide);
  pageHeading.append(headingCopy, showGuide);

  const layout = element("div", "documentation-layout");
  const directory = element("aside", "panel documentation-directory");
  const search = element("form", "documentation-search");
  search.setAttribute("role", "search");
  search.setAttribute("aria-label", "Documentation search");
  const label = element("label", "field");
  const input = element("input");
  input.type = "search";
  input.name = "topic";
  input.autocomplete = "off";
  input.placeholder = "Try reimbursements or CSV";
  label.append(element("span", "", "Search topics"), input);
  const count = element("p", "documentation-search-count");
  count.setAttribute("role", "status");
  search.append(label, count);
  const navigation = element("nav", "documentation-topics");
  navigation.setAttribute("aria-label", "Documentation topics");
  directory.append(search, navigation);
  const article = element("article", "panel documentation-article");
  article.setAttribute("aria-labelledby", "documentation-article-title");
  layout.append(directory, article);
  root.replaceChildren(pageHeading, layout);
  let current = home;
  let results = [];

  function renderDirectory() {
    const query = searchText(input.value).split(" ").filter(Boolean);
    results = [];
    navigation.replaceChildren();
    for (const group of topicGroups) {
      const matched = group.topics.filter(topic => query.every(term => topicText.get(topic.id).includes(term)));
      if (!matched.length) continue;
      const groupPanel = element("div", "documentation-topic-group");
      groupPanel.append(element("p", "documentation-group-label", group.title));
      const groupList = element("ul");
      for (const topic of matched) {
        const anchor = topicLink(topic, "documentation-topic");
        if (topic.id === current) anchor.setAttribute("aria-current", "page");
        groupList.append(element("li", "", anchor));
        results.push(anchor);
      }
      groupPanel.append(groupList);
      navigation.append(groupPanel);
    }
    count.textContent = query.length
      ? (results.length ? results.length + (results.length === 1 ? " matching topic" : " matching topics")
        : "No matching topics. Try a shorter phrase.")
      : "";
  }

  input.addEventListener("input", renderDirectory);
  search.addEventListener("submit", event => {
    event.preventDefault();
    results[0]?.focus();
  });

  function demoActions(demos) {
    const actions = element("div", "documentation-actions");
    const errorMessage = element("p", "notice documentation-error");
    errorMessage.setAttribute("role", "alert");
    errorMessage.hidden = true;
    const buttons = demos.map(demo => {
      const control = button(demo.label, "secondary", "workspace");
      control.addEventListener("click", async () => {
        for (const button of buttons) button.disabled = true;
        errorMessage.hidden = true;
        try {
          await openDemo(demo.view, demo.role);
        } catch (error) {
          const message = apiMessage(error);
          if (!root.hidden && article.contains(errorMessage)) {
            errorMessage.textContent = message;
            errorMessage.hidden = false;
          } else {
            notify(message);
          }
        } finally {
          for (const button of buttons) button.disabled = false;
        }
      });
      return control;
    });
    actions.append(...buttons);
    return [actions, errorMessage];
  }

  function open(slug) {
    const topic = topics.get(slug) ?? topics.get(home);
    current = topic.id;
    renderDirectory();
    const header = element("header", "documentation-article-header");
    const title = element("h2", "", topic.title);
    title.id = "documentation-article-title";
    title.tabIndex = -1;
    header.append(title, element("p", "documentation-summary", topic.summary));
    const content = element("div", "documentation-copy");
    for (const section of topic.sections) {
      const block = element("section", "documentation-section");
      block.append(element("h3", "", section.title));
      for (const paragraph of section.copy ?? []) block.append(element("p", "", paragraph));
      if (section.steps) block.append(list(section.steps, "ol"));
      if (section.bullets) block.append(list(section.bullets, "ul"));
      content.append(block);
    }
    if (topic.example) {
      const example = element("aside", "documentation-example");
      const caption = element("h3", "", icon("line"));
      caption.append(" Example");
      example.append(caption, element("p", "", topic.example));
      content.append(example);
    }
    if (topic.view) {
      const target = link(topic.view[1], "#" + topic.view[0], "text-link");
      target.append(icon("arrow-right"));
      content.append(target);
    }
    if (topic.demos) content.append(...demoActions(topic.demos));
    if (topic.related) {
      const related = element("aside", "documentation-related");
      related.append(element("h3", "", "Related topics"),
        ...topic.related.map(id => topicLink(topics.get(id), "documentation-related-link")));
      content.append(related);
    }
    article.replaceChildren(header, content);
    if (slug) title.focus({ preventScroll: true });
  }

  return { open, maybeShowGuide: guide.maybeShowGuide, showGuide: guide.showGuide, onNavigate: guide.onNavigate };
}
