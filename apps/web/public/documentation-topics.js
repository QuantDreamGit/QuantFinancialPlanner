// The handbook is static product copy. IDs are the stable documentation URLs.
export const topicGroups = [
  {
    title: "Get started",
    topics: [
      {
        id: "getting-started",
        title: "Start tracking your finances",
        summary: "Set up the ledger once, then use the dashboard to see what changed and what you can afford.",
        sections: [
          {
            title: "Build your first useful view",
            steps: [
              "Choose your personal workspace in the top bar. In Accounts, add the balances you want to track with the date you want to start.",
              "Add transactions as they happen or import your bank statement. Use categories for broad spending groups and labels for details such as a trip or a project.",
              "Add your next payday, bills, subscriptions, and regular investment contributions in Forecast. Open Cash runway to see how the timing affects your balance.",
            ],
          },
          {
            title: "How your records feed the dashboard",
            copy: [
              "Account opening balances and recorded entries give you an ongoing ledger. Overview, Money flow, Insights, and goal balances update as you record or correct activity.",
              "Future commitments, planned reimbursements, and scenarios feed the relevant forecasts. Record or match an occurrence when it actually happens to connect the plan to your ledger.",
            ],
          },
          {
            title: "Start with sample data",
            copy: ["The sample workspaces give you a filled-in ledger and upcoming events to explore. Your edits persist there so you can return to an exercise later."],
          },
        ],
        related: ["sample-workspaces", "accounts-and-transactions", "cash-runway"],
      },
      {
        id: "sample-workspaces",
        title: "Practice in a sample workspace",
        summary: "Explore the full workflow with a private set of fictional accounts and transactions.",
        sections: [
          {
            title: "Choose a sample",
            copy: [
              "Sample workspace includes personal activity such as paydays, spending, savings, investments, and upcoming commitments. Start with Overview, then follow a category branch in Money flow to its entries.",
              "Sample Family combines a fictional partner profile with joint accounts. Use it to explore group limits, shared reimbursements, and transfers between accounts in Funding check.",
            ],
          },
          {
            title: "Experiment and come back",
            copy: [
              "Sample workspaces are private to your sign-in and editable. Quant creates the fictional history on first open and keeps your edits for the next visit. The workspace selector at the top shows which ledger you are viewing.",
              "Choose your regular personal or group workspace to work with your own data. Use Manage → Workspaces to reopen either sample. Sample records stay in the corresponding sample ledgers.",
            ],
          },
        ],
        demos: [
          { label: "Open sample workspace", view: "overview", role: "personal" },
          { label: "Open Sample Family", view: "overview", role: "family" },
        ],
        related: ["overview-and-trends", "funding-check", "family-workspaces"],
      },
    ],
  },
  {
    title: "Understand your money",
    topics: [
      {
        id: "overview-and-trends",
        title: "Overview and financial trends",
        summary: "Read your current balances and compare recorded activity over time.",
        keywords: "wealth line charts barplots stacked charts dashboard",
        view: ["overview", "Open Overview"],
        sections: [
          {
            title: "Read the overview",
            copy: [
              "The net worth card shows current tracked wealth. Income, Expenses, and Invested summarize the selected period. Account balances shows how your current funds are distributed.",
              "Needs attention surfaces items such as upcoming bills, overdue scheduled reimbursements, import rows still in review, group spending alerts, and a projected low in the next 30 days.",
            ],
          },
          {
            title: "Change the chart and horizon",
            steps: [
              "Pick a period such as 1M for the last 30 days or 6M for six calendar months including the current partial month. The choice also sets the period for the flow map.",
              "Choose Wealth for tracked balances, Cash flow for period activity, or Combined to compare movement and balances together.",
              "Choose a line chart to follow a trend, vertical bars to compare periods, or stacked bars to see how components add up. Select a legend item to toggle it.",
              "Hover or use the arrow keys on the chart to inspect periods. Open Data for the underlying numbers.",
            ],
          },
        ],
        related: ["flow-and-replay", "insights", "cash-runway"],
      },
      {
        id: "flow-and-replay",
        title: "Money flow and replay",
        summary: "Follow the flow from total income, expenses, or investments to the categories and transactions behind each amount.",
        view: ["flow", "Open Money flow"],
        sections: [
          {
            title: "Explore the current flow map",
            copy: [
              "Choose all flows or focus on one type. Expand a total to view its categories, then expand a category to see its children or transactions. The colors identify flows and categories, while the line widths reflect amounts.",
              "Select a node to inspect its amount and entries. Use Fullscreen for a wide map or Reset view to clear expansion. Expanded branches are remembered in the current workspace.",
            ],
          },
          {
            title: "Compare months in replay",
            steps: [
              "Open Replay months from Money flow and use the scrubber to inspect a recorded month.",
              "Pin a month, then move the scrubber to another month. Both maps use the same scale for line widths.",
              "Inspect the biggest differences to find the branches that changed most. Open a branch’s transactions to identify the entries that explain it.",
            ],
          },
        ],
        example: "Compare a summer month with the previous one and inspect a wider Travel branch. Its transactions can show whether the change came from a single booking or several smaller charges.",
        related: ["categories-and-labels", "overview-and-trends", "insights"],
      },
      {
        id: "insights",
        title: "What changed and Spending detective",
        summary: "Reconcile month-to-month changes in net worth and investigate unusual spending.",
        keywords: "waterfall anomaly anomalies price increase gain loss",
        view: ["insights", "Open Insights"],
        sections: [
          {
            title: "Read the net worth waterfall",
            copy: [
              "Choose a month. The waterfall starts from the previous month’s balance and adds the recorded drivers of change, including income, expenses, investment value changes, and newly opened accounts.",
              "Select a bar to inspect its entries. Transfers and investment contributions move value between tracked accounts, so they preserve tracked net worth.",
            ],
          },
          {
            title: "Inspect a detective flag",
            copy: [
              "Spending detective looks for new recurring charges, increases to recurring payments, and categories with unusually high spending. Open the flag to see the transactions and baseline that triggered it.",
              "Repeated entries and sufficient recorded history help establish a baseline. Check descriptions, dates, and categories before deciding whether the pattern represents a lasting change.",
            ],
          },
        ],
        example: "If Home spending jumps, open the expense step and the detective flag. A recorded annual insurance renewal may explain the difference without changing the usual monthly subscription.",
        related: ["flow-and-replay", "categories-and-labels", "plan-vs-actual"],
      },
    ],
  },
  {
    title: "Record and reconcile",
    topics: [
      {
        id: "accounts-and-transactions",
        title: "Accounts and transactions",
        summary: "Keep balances and the activity behind them in a persistent ledger.",
        view: ["accounts", "Open Accounts"],
        sections: [
          {
            title: "Create accounts and opening balances",
            copy: [
              "In Manage → Accounts, add checking, cash, savings, or investment accounts in euros. Enter the balance immediately before the first transactions you intend to record and use an opening date that covers that history.",
              "If you later bring in older records, update the opening date and opening balance together. An opening balance should exclude the activity you will import or record after that starting point.",
            ],
          },
          {
            title: "Choose the correct transaction type",
            bullets: [
              "Income and expense record money earned or spent from an account. Add a date and description, then a category and any relevant expense labels.",
              "Transfer moves money between tracked accounts, for example from checking to a savings account.",
              "Investment contribution moves cash into an investment account and shows in the investment flow. Investment value change records market gains or losses in the tracked value.",
            ],
          },
          {
            title: "Record cash you already have or withdraw",
            steps: [
              "In Manage → Accounts, select Add cash wallet and enter the cash you physically have as its opening balance.",
              "Use Record cash expense on the wallet card for purchases made in cash. Those purchases reduce the wallet balance and default to outside the statement.",
              "Use Record cash withdrawal to move money from a bank account to the wallet. Then record the cash purchases as expenses. When the withdrawal appears on the bank statement, match it to the transfer.",
            ],
          },
          {
            title: "Find or update recorded activity",
            copy: ["Open Activity → Transactions to filter by account, type, category, or search text. Opening entries from Money flow or Replay also scopes the ledger to that period. Record future occurrences through Forecast once they happen, and use Shared payments to manage a bill with split shares."],
          },
        ],
        example: "Withdraw €100 from checking into your cash wallet, then spend €18 in cash. The withdrawal moves €100 between balances. The purchase records €18 of spending and leaves €82 in the wallet.",
        related: ["categories-and-labels", "statement-imports", "shared-payments", "transaction-attachments"],
      },
      {
        id: "transaction-attachments",
        title: "Attach receipts and invoices to a transaction",
        summary: "Store supporting documents with the ledger entry they belong to.",
        keywords: "paperclip receipt invoice proof document pdf file attachment",
        view: ["transactions", "Open Transactions"],
        sections: [
          {
            title: "Attach documents",
            steps: [
              "In Activity → Transactions, choose Files on a saved entry to open its documents directly. Choose Attach files if it has none yet.",
              "Choose Attach documents, select the files and choose Save files. You can also add files when creating or editing a transaction.",
              "In Files, use Download PDF or Download beside the document. If an upload fails, the pending files remain ready for retry.",
            ],
          },
          {
            title: "File limits and access",
            copy: [
              "Each transaction supports up to 12 files of 10 MiB each. Common invoice formats include PDF, images, Office, and OpenDocument.",
              "Open personal attachments from the ledger that owns the entry. Group members can open documents for a joint transaction. Attachment files are stored in the ledger database.",
              "Original bank statements are available under Original PDF statements in Import payments. Transaction files are stored with individual ledger entries.",
            ],
          },
        ],
        related: ["accounts-and-transactions", "statement-imports", "family-workspaces"],
      },
      {
        id: "categories-and-labels",
        title: "Categories, child nodes, and labels",
        summary: "Give transactions a consistent place in the flow map and tag spending that cuts across categories.",
        view: ["categories", "Open Categories"],
        sections: [
          {
            title: "Organize categories into trees",
            copy: [
              "Income, expense, and investment contribution categories have separate trees. Create a top-level category in Manage → Categories, or add a child beneath an existing category of the same type. Set a color to make branches easier to distinguish.",
              "Choose Add starter categories for preset groups such as Employment, Groceries, and Index funds. You can also choose the preset when creating a workspace.",
              "A parent’s reported amount includes its children. Use a child when you want to identify a type of activity separately while still counting it in the parent total.",
            ],
          },
          {
            title: "Add reusable expense labels",
            copy: ["Apply several labels to the same expense when useful. For example, place a hotel charge in Travel → Accommodation and apply the labels Summer holiday and Reimbursable. Filter your transactions by the detail you need to find."],
          },
        ],
        related: ["accounts-and-transactions", "flow-and-replay", "family-limits"],
      },
      {
        id: "statement-imports",
        title: "Import bank statements and Amazon orders",
        summary: "Review file-based transactions against your ledger before you record or match them.",
        view: ["imports", "Open Import payments"],
        sections: [
          {
            title: "Choose a file type",
            bullets: [
              "For a bank or card CSV, select the ledger account, then map the date and description and either the signed amount or separate debit and credit columns. Check the source’s date and sign conventions. Map the transaction reference when available.",
              "For PDF statements, choose Local extraction for text-based files or AI with OpenAI for full PDF extraction, including scanned pages. Choose all files for the same ledger account together. On macOS, the local OCR option can also read scans.",
              "For Amazon, request your retail order history through your Amazon account’s Request My Data feature. Select the resulting order-history CSV or ZIP. Quant also accepts physical-order JSON from Amazon Data Portability.",
            ],
          },
          {
            title: "Review and submit the table",
            steps: [
              "Import statements for each account separately. Edit posting dates, descriptions, signed amounts, and categories in the table. Verified matches preserve the existing transaction’s category, labels, notes, and documents.",
              "Use Details to compare a row with its source or a proposed match. Choose how to handle ambiguous matches and use Skip row to leave out a transaction.",
              "Select Import transactions to submit the table. Quant clears the preview and confirms how many transactions were imported. Open Recent imports to see what was recorded.",
            ],
          },
          {
            title: "Match an expense you entered by hand",
            copy: [
              "Leave This transaction will appear on a statement selected for bank expenses you expect to import. This is the default for checking or savings. Clear it for an entry you keep outside the bank statement.",
              "Quant verifies an expense automatically when the account, date, signed amount and descriptions agree, with no competing candidate. Date differences and ambiguous descriptions remain available for review.",
            ],
          },
          {
            title: "Download the PDF you uploaded",
            copy: [
              "After PDF extraction, Download original PDF is available beside the preview and under Original PDF statements on the Import payments page. Select the original by filename, account and date.",
              "The archive starts with newly extracted PDFs. Re-upload an older original if you want to keep a copy here.",
            ],
          },
        ],
        example: "For a bank debit that already appears as an account transfer, choose the proposed transfer to reconcile it. Adding a second expense would misstate both cash and spending.",
        related: ["openai-pdf-import", "groq-pdf-import", "accounts-and-transactions", "shared-payments", "cash-runway"],
      },
      {
        id: "openai-pdf-import",
        title: "Read and summarize PDFs with OpenAI",
        summary: "Review multiple statements, keep the originals, and separate large expenses from monthly totals.",
        view: ["imports", "Open Import payments"],
        sections: [
          {
            title: "Set up the key and select your documents",
            steps: [
              "Choose PDF statements and AI with OpenAI. Paste an OpenAI API key and select Save key, or let Quant save it after a successful extraction. Use Change key or Remove key later in the same form.",
              "Choose the ledger account and select its PDFs together. Confirm that Quant can send the entire files to OpenAI. Quant saves a copy of each original under Original PDF statements.",
            ],
          },
          {
            title: "Read the summary and check the transactions",
            copy: [
              "Quant lists expenses over €250 individually with their descriptions. It groups expenses of €250 or less by posting month and reports credits separately. Applying the import keeps the original entries and dates in the ledger so you can reconcile them and use them in daily cash forecasts.",
              "Quant compares the reported printed debit and credit totals with the sum of the extracted rows when those totals are present. Check uncertain dates, currencies, descriptions, and possible duplicates against the original. The JSON export includes source filenames, available page numbers, and short evidence excerpts.",
              "An unnamed import uses Bill - YYYY-MM-DD. Open a completed batch from Recent imports to see a summary of what was applied and retrieve its source PDFs.",
            ],
          },
        ],
        example: "A €310 insurance premium appears on its own. Three smaller expenses of €250, €24.50 and €5.50 in the same month appear as a €280 group. Total expenses are €590.",
        related: ["statement-imports", "groq-pdf-import", "accounts-and-transactions"],
      },
      {
        id: "groq-pdf-import",
        title: "Optional AI PDF extraction with Groq",
        summary: "Send locally extracted statement text to Groq when you opt in to AI extraction for a PDF.",
        view: ["imports", "Open Import payments"],
        sections: [
          {
            title: "Connect a key and prepare a statement",
            steps: [
              "Create an API key in your Groq Console. In Import payments, choose PDF statement and AI with Groq. Paste a key and use Save key, or let Quant save it after a successful AI extraction.",
              "Select the account, upload the PDF, and optionally give the import a name. Confirm for this file that Quant can send the extracted text to Groq. The original PDF stays on the Quant server.",
              "Review the extracted dates, descriptions, signed amounts, and currency in the table. Choose Skip row for any transaction you want to leave out, then submit the table.",
            ],
          },
          {
            title: "Manage your key and review exports",
            copy: [
              "A saved key belongs to your sign-in and follows you across your workspaces. Use Change key or Remove key in the import form. If you have no personal key, Quant can use a Groq key configured by the server owner.",
              "An unnamed PDF import defaults to Bill - YYYY-MM-DD. Descriptions in the statement remain on their transactions. You can download the normalized CSV or JSON for review. Normalized CSV uses Date, Description, Amount, Currency, and Reference, with negative debits and positive credits.",
              "For long statements, choose a smaller PDF or a bank CSV if extraction reaches the provider limit. Local extraction remains available in the form.",
            ],
          },
        ],
        related: ["statement-imports", "family-workspaces"],
      },
      {
        id: "shared-payments",
        title: "Shared payments and reimbursements",
        summary: "Split a bill with tagged people and track the difference between cash paid and your share of spending.",
        keywords: "Splitwise debt repayment money owed friends joint subscription settle",
        view: ["shared", "Open Shared payments"],
        sections: [
          {
            title: "Record a split",
            steps: [
              "In Activity → Shared payments, add each person and choose a tag. Under Add shared payment, enter the amount, date, category, and who paid.",
              "Select the people included in the split. Use equal shares or enter custom amounts. Give a recurring bill its weekly, monthly, or yearly schedule and an optional end date.",
              "Record the occurrence when the payment happens. Cash paid shows the full amount that left your account. Your share is what counts as your spending. Owed back or You owe shows the amount to settle.",
            ],
          },
          {
            title: "Settle or schedule repayment",
            copy: [
              "Use the balance beside a person to record a reimbursement you received or a repayment you made. The settlement moves cash and reduces what is owed. The original expense still supplies the spending amount.",
              "Schedule a future reimbursement or repayment to include it in Cash runway and Funding check. Record or reconcile it against the import when it arrives. An outstanding balance contributes to tracked wealth and enters the cash forecast when it has a scheduled date.",
            ],
          },
        ],
        example: "You pay €90 for yourself and two friends in equal shares. Cash paid is €90, Your share is €30, and Owed back totals €60. Two €30 repayments replenish cash and clear the amount owed.",
        related: ["statement-imports", "cash-runway", "funding-check", "family-limits"],
      },
    ],
  },
  {
    title: "Plan and test decisions",
    topics: [
      {
        id: "cash-runway",
        title: "Cash runway and commitments",
        summary: "See the days when income, bills, transfers, and reimbursements are expected to move cash.",
        view: ["forecast", "Open Forecast"],
        sections: [
          {
            title: "Build a calendar of expected cash",
            copy: [
              "Use Planning → Forecast → Cash runway to add paydays, bills, subscriptions, contributions, and other commitments. Choose the account, amount, frequency, and first date. Scheduled shared payments, settlements, and eligible returns also feed the runway.",
              "Read the actual and projected daily cash lines together. Select a calendar day to see the movements behind its closing balance. A large bill before payday can make a week tight even when the month closes with enough cash.",
            ],
          },
          {
            title: "Keep scheduled and posted cash in sync",
            copy: [
              "Post a due occurrence when it happens, match it to an existing ledger transaction, or skip it if it did not occur. Matching keeps an already recorded movement from counting as a second scheduled payment.",
              "Choose Check funding beside an upcoming item to test its paying account, reserve, and approved transfer options. In a group, manage a member’s schedule from the corresponding personal workspace.",
            ],
          },
        ],
        related: ["what-if", "monthly-returns", "shared-payments", "funding-check"],
      },
      {
        id: "what-if",
        title: "What-if simulator",
        summary: "Compare the same starting balances and commitments under two versions of a decision.",
        view: ["forecast", "Open Forecast"],
        sections: [
          {
            title: "Run a baseline and alternative",
            steps: [
              "Open Planning → Forecast → What-if simulator. Choose your horizon and enter the reserve level you want to protect.",
              "Enter the purchase amount and baseline date. Move the sliders to change the timing, a recurring cost such as rent, or monthly investment contributions in the alternative. Choose Update comparison to plot both paths.",
              "Check each path’s first reserve breach and lowest cash balance. Save the comparison with a name so you can reopen it as recorded balances become available.",
            ],
          },
        ],
        example: "Test buying a laptop the week before payday and one month later. Compare the first reserve breach and lowest projected cash for each date, then save the scenario you want to revisit.",
        related: ["cash-runway", "purchase-planner", "plan-vs-actual", "decision-lab"],
      },
      {
        id: "purchase-planner",
        title: "Purchase planner",
        summary: "Compare upfront and gradual funding for a major purchase.",
        view: ["planner", "Open Purchase planner"],
        sections: [
          {
            title: "Set up a comparison",
            copy: [
              "Enter starting cash and savings, expected monthly cash flow, the purchase price and target date, and the reserve you want to retain. Use ledger values to prefill tracked balances and the average change from activity in the last three full months.",
              "The planning engine compares setting funds aside upfront with setting them aside over time. Inspect the schedule’s Available and Earmarked columns and the month the purchase occurs.",
            ],
          },
          {
            title: "Record your assumptions",
            copy: ["Save a named comparison and add monthly category estimates if you want to explain variance later. Use Check funding to test the actual paying account and approved transfer routes before deciding on a purchase date."],
          },
        ],
        related: ["plan-vs-actual", "what-if", "funding-check"],
      },
      {
        id: "plan-vs-actual",
        title: "Plan vs. actual",
        summary: "Reopen a saved purchase or what-if plan and compare the original forecast with recorded cash.",
        view: ["forecast", "Open Forecast"],
        sections: [
          {
            title: "Reopen and compare",
            steps: [
              "Save a purchase comparison in Purchase planner or a scenario in the What-if simulator before the period you want to assess.",
              "Later, open Planning → Forecast → Plan vs. actual and select that saved plan. The chart overlays recorded month-end cash and savings for completed months with data, within the plan’s original account scope.",
              "Compare the gap month by month. For purchase plans with estimates, use the category comparison to identify which spending or income assumptions diverged from recorded activity.",
            ],
          },
          {
            title: "Interpret differences",
            copy: ["An earlier expense, a delayed payment, or a different amount can all produce a balance gap. Check the entries and category differences before deciding which assumption to adjust in your next plan."],
          },
        ],
        example: "A saved plan expected €2,400 at month-end and the ledger shows €2,050. Compare the categories to see whether the €350 gap follows a higher utility bill, a smaller paycheck, or other entries in the period.",
        related: ["purchase-planner", "what-if", "forecast-scorecard", "insights"],
      },
      {
        id: "goals-and-savings",
        title: "Savings goals and contribution trade-offs",
        summary: "Connect goals to the accounts funding them and see how competing contributions affect completion dates.",
        view: ["goals", "Open Goals"],
        sections: [
          {
            title: "Read the savings map",
            copy: [
              "Create a goal with a target amount and optional target date. Choose one or more savings accounts and set the funds and monthly contributions assigned to the goal.",
              "The map shows the links from each account to its goals, current progress, and an estimated completion date when the forecast reaches the target. Projections use up to five years of scheduled movements in the linked accounts.",
            ],
          },
          {
            title: "Compare competing goals",
            copy: [
              "Preview redirecting part of a monthly contribution between goals that share a funding account. Watch how the earlier completion of one goal changes the other goal’s date.",
              "Decision lab → Goal trade-off plots completion dates across several redirection amounts. In a family workspace, member goal allocations can compete with shared goals for the same savings.",
            ],
          },
        ],
        example: "Try redirecting €50 per month from a holiday to a car goal. Look at both dates and the account’s scheduled withdrawals before choosing the contribution.",
        related: ["monthly-returns", "decision-lab", "funding-check", "family-workspaces"],
      },
      {
        id: "monthly-returns",
        title: "Monthly interest and fixed-income returns",
        summary: "Schedule recurring deposit interest or coupon income and show where it will land.",
        view: ["returns", "Open Monthly returns"],
        sections: [
          {
            title: "Define the payment",
            steps: [
              "In Planning → Monthly returns, choose a savings or investment account and enter a name and the first payment date.",
              "Enter a fixed monthly amount or a monthly percentage on a fixed principal. Update the schedule when a rate or principal changes.",
              "Choose whether to keep the return in the instrument account or send it to a tracked checking, cash, or savings account. Set an end date if known.",
            ],
          },
          {
            title: "Record the actual receipt",
            copy: [
              "Cash and savings payouts flow into the cash forecast. A payment retained in an investment account increases the tracked investment balance when recorded.",
              "When the return appears on a statement, record it from the runway or match it to the transaction in your account. This also clears the pending occurrence.",
            ],
          },
        ],
        example: "A 0.3% monthly return on a fixed €5,000 principal generates €15 per month. The amount stays €15 on that schedule if the account balance changes.",
        related: ["cash-runway", "goals-and-savings", "statement-imports"],
      },
      {
        id: "decision-lab",
        title: "Risk, reverse planning, and the optimizer",
        summary: "Test how cash reserves and goals respond to uncertainty and to choices you can make.",
        view: ["decision", "Open Decision lab"],
        sections: [
          {
            title: "Choose risk assumptions",
            copy: [
              "Risk simulator runs daily cash paths from your scheduled commitments. Adjust the horizon, number of paths, reserve, purchase amount and date, and uncertainty in income, expenses, delayed reimbursements, or surprise costs.",
              "Historical sampling can draw unscheduled activity from completed months. Read the median and percentile band together with the share of paths that keep the reserve. Changing assumptions changes that result.",
            ],
          },
          {
            title: "Find headroom and test trade-offs",
            bullets: [
              "Use Reverse planner to see the maximum purchase amount by date at the selected reserve success target. Inspect stress results and ranked drivers to see which assumptions weigh most on cash.",
              "Decision optimizer compares available purchase dates, investment contributions, and goal reallocations on a finite grid. Look at reserve margins and completion dates before selecting an option.",
              "Use Goal trade-off to compare the projected effect of redirecting shared monthly savings between goals.",
            ],
          },
        ],
        related: ["reverse-planner", "decision-optimizer", "goal-tradeoff", "forecast-scorecard"],
      },
      {
        id: "reverse-planner",
        title: "Find your purchase capacity by date",
        summary: "Use the modeled cash paths to estimate a maximum purchase that meets your reserve success target.",
        view: ["decision", "Open Decision lab"],
        sections: [
          {
            title: "Read the reverse planner",
            steps: [
              "Open Planning → Decision lab → Risk simulator. Choose a reserve and a reserve success target, then run the simulation.",
              "Open Reverse planner in the results and inspect a date. The chart estimates how much room the modeled paths leave for a purchase on that date.",
              "Use the stress results and ranked drivers to see what shifts capacity. Run again with a different assumption to compare.",
            ],
          },
          {
            title: "Interpret a result",
            copy: ["Each estimate uses the selected assumptions and the full modeled horizon. A purchase date can look affordable at first and still leave too little cash for a later payment."],
          },
        ],
        related: ["decision-lab", "funding-check", "decision-optimizer"],
      },
      {
        id: "decision-optimizer",
        title: "Compare feasible combinations of decisions",
        summary: "Explore purchase dates, investment contributions, and savings reallocations together.",
        view: ["decision", "Open Decision lab"],
        sections: [
          {
            title: "Define the choices to compare",
            copy: ["Open Planning → Decision lab → Decision optimizer. Set the purchase window and the contribution changes you want to compare. Include a goal redirection if two goals share an eligible account."],
          },
          {
            title: "Compare the feasible options",
            copy: ["The optimizer evaluates a finite grid against the selected reserve and success threshold. Compare payment date, contribution amount, reserve margin, and goal timings in the results. Change the input grid when another trade-off matters to you."],
          },
        ],
        related: ["decision-lab", "reverse-planner", "goal-tradeoff"],
      },
      {
        id: "goal-tradeoff",
        title: "Compare redirects between two goals",
        summary: "See how shifting monthly savings affects both goals’ completion dates.",
        view: ["decision", "Open Decision lab"],
        sections: [
          {
            title: "Compare goals on the same account",
            steps: [
              "Open Planning → Decision lab → Goal trade-off and select two goals linked to the same funding account.",
              "Choose the direction of the redirect and inspect the dates at different monthly amounts. A gap in a result means that goal does not reach its target in the modeled horizon.",
              "Open Goals to review the current earmarks or save a change to future contributions.",
            ],
          },
        ],
        related: ["goals-and-savings", "decision-optimizer", "funding-check"],
      },
      {
        id: "forecast-scorecard",
        title: "Forecast scorecard",
        summary: "Measure how saved forecasts held up as actual balances arrived.",
        view: ["decision", "Open Decision lab"],
        sections: [
          {
            title: "Compare using the original account scope",
            copy: [
              "Open Decision lab → Forecast scorecard to compare saved purchase plans, what-if baselines, and risk forecasts with later ledger cash. Each comparison uses the accounts included when the forecast was saved.",
              "A positive signed error means recorded cash exceeded the forecast. Absolute error shows the size of the gap in euros. For risk forecasts, coverage shows how often recorded cash fell within the saved 10th–90th percentile band.",
            ],
          },
          {
            title: "Combine the score with a closer look",
            copy: ["Compare the errors and coverage over elapsed dates with recorded data. Use Plan vs. actual to investigate a particular purchase or what-if plan in detail and see the entries or category estimates behind the gap."],
          },
        ],
        related: ["plan-vs-actual", "decision-lab"],
      },
      {
        id: "funding-check",
        title: "Funding check and approved transfers",
        summary: "Test whether a particular account can cover a payment while keeping the reserves you set.",
        keywords: "funding coach liquidity minimum maturity routes bridge payment shortfall",
        view: ["funding", "Open Funding check"],
        sections: [
          {
            title: "Set the rules for using each account",
            copy: [
              "In Funding check, set the minimum balance and optional first available date for accounts you manage. A locked investment source needs an explicit availability date to be eligible.",
              "Approve direct transfer routes from accounts you manage into eligible payment accounts. A route is directional and can take zero to 14 business days. Availability, arrival dates, and each account’s minimum balance affect which routes the check can use.",
            ],
          },
          {
            title: "Run the check for a payment",
            steps: [
              "Choose the payment account, amount, proposed date, and forecast horizon. Leave the amount at zero to test only the bills already scheduled.",
              "Read the first shortfall and the payment-account chart. Check the amounts, send dates, and arrival dates in the suggested transfers. A plan can still show a gap when the recorded routes and available balances cannot cover it.",
              "Compare the expected schedule with the separate delayed-reimbursement scenario. These suggestions model the cash moves. Record the transfers in your ledger when you actually make them.",
            ],
          },
        ],
        example: "In Sample Family, select Load sample purchase, then Check funding. The example uses a €900 payment from Family checking 10 days after you first open the sample, a 60-day horizon, and a 21-day reimbursement delay. On the day the sample is created, the suggested transfers total €450 under the expected schedule and €750 under the delay scenario. Later dates and edits may change the amounts. If the sample purchase date has passed, choose a new date and run the check again.",
        demos: [{ label: "Try funding in Sample Family", view: "funding", role: "family" }],
        related: ["sample-workspaces", "cash-runway", "shared-payments", "family-workspaces"],
      },
    ],
  },
  {
    title: "Manage access",
    topics: [
      {
        id: "family-workspaces",
        title: "Personal workspaces and family access",
        summary: "Use your sign-in and workspace selector to switch between personal finances and the groups you share.",
        view: ["workspaces", "Open Workspaces"],
        sections: [
          {
            title: "Choose the ledger",
            copy: [
              "Your personal profile holds your own accounts and records. A group can include member balances, transactions, scheduled cash flows, and goals in its combined view. It can also hold joint accounts and activity.",
              "Choose the intended workspace before entering a payment. Members can record activity in joint accounts and enter a regular transaction in their own account from the group. Use your personal workspace to edit its history, manage schedules, or import statements.",
            ],
          },
          {
            title: "Invite someone to sign in",
            steps: [
              "As the service owner, create a personal profile for the person in Manage → Workspaces and add it to any groups you intend that person to share.",
              "Open Manage → Family access and create an invitation for the profile. Share the link with the intended person. It works once and expires after 48 hours.",
              "The recipient sets up their own sign-in and gets access through that profile’s memberships. In Family access, the service owner can cancel invitations, revoke sessions, or disable a sign-in.",
            ],
          },
          {
            title: "Manage your sign-in",
            copy: ["Use the account menu to change your password or sign out. Group membership controls whose records appear in a group. Private sample workspaces are associated with the sign-in that creates them."],
          },
        ],
        related: ["family-limits", "sample-workspaces", "accounts-and-transactions"],
      },
      {
        id: "family-limits",
        title: "Group spending limits",
        summary: "Set monthly thresholds for shared spending and see how actual and expected expenses compare.",
        view: ["budgets", "Open Group limits"],
        sections: [
          {
            title: "Define a limit and scope",
            copy: [
              "In Planning → Group limits, choose a group and set a monthly cap for the entire group, an individual member, or a category. Caps use the personal share of shared expenses.",
              "Use the group view to compare recorded and projected spending against each cap. Members can see limits that apply to them from their personal workspace. Overview brings actual and projected breaches into Needs attention.",
            ],
          },
        ],
        example: "Set a €450 monthly household expense cap. A €90 shared household bill with a €30 personal share contributes €30 to the corresponding member’s expense total.",
        related: ["family-workspaces", "categories-and-labels", "shared-payments"],
      },
    ],
  },
];
