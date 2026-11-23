# Architecture and design specification

## 1. Project Overview

### 1.1 Purpose

The **Financial Planner** is a C++23 application designed to help a user decide how current and future financial decisions should affect savings, expenses, liquidity, and investments.

The system is not intended to be only a bookkeeping application. Its main purpose is to reason about **financial choices over time**.

A typical question is not merely:

> Can I afford to spend €10,000?

Instead, the Planner should be able to answer questions such as:

> If I want to buy a €10,000 car in six months, what are the possible ways to finance that expense while respecting my liquidity reserve, investment strategy, risk profile, and other future goals?

The Planner may therefore propose several alternatives, for example:

- accumulate the required amount entirely as cash;
- temporarily reduce investment contributions;
- use part of existing savings;
- reserve funds in a low-risk time deposit;
- combine savings and reduced investment contributions;
- preserve investments at the cost of lower liquidity;
- preserve liquidity at the cost of lower investment exposure.

The system should evaluate the consequences of each alternative over time and present them to the user.

The user selects one of the proposed plans. Only after this choice is made can the selected plan be executed against the application's financial state.

---

## 2. Academic and Engineering Goals

The project is intended both as a useful personal application and as an academic software-engineering project suitable for a technical portfolio or curriculum.

The project should demonstrate:

1. domain-driven modelling of a non-trivial financial problem;
2. appropriate use of well-known software design patterns;
3. modern C++23 techniques;
4. static and dynamic polymorphism;
5. value-oriented design;
6. separation between planning and execution;
7. explicit modelling of time, risk, constraints, and preferences;
8. testable architecture based on dependency injection;
9. extensibility toward external financial services such as Interactive Brokers;
10. clear justification of architectural decisions and rejected alternatives.

The objective is **not** to insert as many design patterns as possible.

Patterns should be used only where the domain presents a genuine variation point or architectural problem.

A central academic principle of the project is therefore:

> Patterns should emerge from the problem rather than being forced into the implementation.

---

# 3. High-Level Functional Idea

The system receives a description of the user's current financial position and future intentions.

Examples of input include:

- current cash;
- savings accounts;
- investment positions;
- recurring income;
- recurring expenses;
- discretionary expenses;
- minimum liquidity requirements;
- future known expenses;
- goals;
- investment preferences;
- risk configuration;
- optimization preferences.

The Planner then reasons about how a proposed decision affects future financial states.

Conceptually:

```text
FinancialState(now)
        |
        v
FinancialState(t1)
        |
        v
FinancialState(t2)
        |
       ...
        |
        v
FinancialState(goal_date)
```

The financial problem is therefore intrinsically time-dependent.

A decision made today can affect:

- available cash;
- future monthly savings;
- investment contributions;
- portfolio value;
- expected investment return;
- the ability to satisfy later goals;
- the amount of money that must remain liquid;
- exposure to financial risk.

---

# 4. Core Architectural Principle

The main architecture separates four responsibilities:

```text
Planner       = proposes what could be done
Optimizer     = searches and ranks alternatives
RiskManager   = evaluates risk
Engine        = executes the selected actions
```

The authoritative financial state is kept separately:

```text
FinancialState = source of truth
```

The most important architectural invariant is:

> **Only the Engine is allowed to mutate the authoritative financial state.**

The Planner, Optimizer, and RiskManager operate on immutable snapshots.

This creates a strong separation between:

- analysis;
- recommendation;
- user decision;
- execution.

---

# 5. High-Level Architecture

```text
                           GUI
                            |
                            v
                         Planner
                            |
                  +---------+---------+
                  |                   |
                  v                   v
               Optimizer          RiskManager
                  |                   |
        +---------+---------+         |
        |                   |         |
        v                   v         v
OptimizationPolicy        Solver   RiskStrategy
                  |
                  v
            Candidate Plans
                  |
                  v
               Planner
                  |
                  v
              Ranked Plans
                  |
                  v
                 GUI
                  |
           user selects one
                  |
                  v
               Commands
                  |
                  v
                Engine
                  |
        +---------+---------+
        |                   |
        v                   v
      Broker          FinancialRepository
        |                   |
        v                   v
      Trades         FinancialState
```

---

# 6. Main Application Flow

The main application workflow is:

```text
FinancialSnapshot
        +
       Goal
        +
      Config
        |
        v
     Planner
        |
        v
 Candidate Plans
        |
        +------> RiskManager
        |
        +------> Optimizer
        |
        v
   Ranked Plans
        |
        v
       GUI
        |
   user chooses
        |
        v
     Commands
        |
        v
      Engine
        |
        v
Transactions / Trades
        |
        v
 FinancialState
```

Planning and execution are deliberately separate.

The Planner may generate plans that are never executed.

The Engine receives only the plan or commands that the user has actually accepted.

---

# 7. Domain Model

## 7.1 Value-Oriented Domain

The domain should strongly prefer explicit value types instead of primitive values such as raw `double`, `int`, or `std::string`.

Instead of:

```cpp
double money;
double risk;
int priority;
double time;
```

the project should introduce domain types such as:

```cpp
Money
Currency
Quantity
Risk
Priority
Duration
Date
Percentage
InterestRate
```

This prevents primitive obsession and allows the compiler to enforce domain rules.

---

## 7.2 Money

Money should be a strong domain type.

A possible representation is:

```cpp
enum class Currency {
    EUR,
    USD,
    GBP
};

struct Money {
    std::int64_t minor_units;
    Currency currency;
};
```

Using integer minor units avoids many floating-point issues for ordinary monetary values.

Potential operations include:

```cpp
Money + Money
Money - Money
Money * Percentage
Money / scalar
```

Operations between different currencies should either be rejected or require an explicit exchange-rate service.

---

# 8. Financial State

`FinancialState` represents the authoritative current state of the user's finances.

It may contain:

```text
FinancialState
|
+-- Cash accounts
+-- Savings accounts
+-- Deposits
+-- Investment accounts
+-- Positions
+-- Recurring incomes
+-- Recurring expenses
+-- Scheduled transactions
+-- Active goals
+-- Historical transactions
```

The exact internal structure can evolve as the implementation matures.

The central rule remains:

> Components involved in planning receive a read-only snapshot, not mutable access to the real state.

---

## 8.1 FinancialSnapshot

The Planner should use:

```cpp
FinancialSnapshot snapshot = repository.snapshot();
```

rather than direct mutable access to `FinancialState`.

The following components should receive only:

```cpp
const FinancialSnapshot&
```

where practical:

```text
Planner
Optimizer
RiskManager
Solver
Strategies
```

This allows hypothetical simulations without changing the real financial state.

---

# 9. Transactions

A transaction records a financial event that has actually occurred or has been committed to the state.

Potential transaction categories include:

```text
Income
Expense
Saving
Investment
Transfer
Interest
Fee
```

The project should avoid assuming immediately that all transaction categories require inheritance.

Possible representations include:

### Runtime inheritance

```cpp
class Transaction {
public:
    virtual ~Transaction() = default;
};
```

### Tagged template types

```cpp
template<typename Kind>
class Transaction;
```

### `std::variant`

```cpp
using Transaction = std::variant<
    IncomeTransaction,
    ExpenseTransaction,
    SavingTransaction,
    InvestmentTransaction,
    TransferTransaction
>;
```

For a closed set of transaction types, `std::variant` is likely to be a strong C++23 choice.

The final choice should be driven by the expected extension model.

---

# 10. Goals Are Not Transactions

A major modelling decision is that a financial goal should **not** be represented as a transaction.

For example:

> Buy a €10,000 car in six months.

This is not yet a financial event.

It is a future requirement.

A possible model is:

```cpp
struct Goal {
    Money target;
    Date due_date;
    Priority priority;
    Flexibility flexibility;
};
```

More specialized types may later be introduced:

```cpp
struct FutureExpense;
struct SavingsTarget;
struct WealthTarget;
struct EmergencyFundTarget;
```

The Planner converts a goal into one or more possible plans.

---

# 11. Future Expenses

A future expense represents a known or anticipated expense at a future date.

Example:

```text
Car purchase
Target amount: €10,000
Due date: 6 months
Priority: High
Flexibility: ±1 month
```

A future expense does not immediately reduce the current balance.

Instead, it constrains the planning problem.

---

# 12. Plan as a First-Class Domain Object

`Plan` is one of the most important objects in the architecture.

A Plan represents one possible way to satisfy one or more goals.

Example:

```cpp
struct Plan {
    std::vector<ScheduledCommand> commands;
    FinancialProjection projection;
    RiskAssessment risk;
    PlanScore score;
};
```

A Plan may include:

- immediate actions;
- monthly actions;
- future actions;
- investment changes;
- savings allocations;
- transfers;
- deposit creation;
- expected state evolution;
- risk metrics;
- plan score.

---

## 12.1 Example Plans

Assume:

```text
Goal:
Buy a car for €10,000 in 6 months.
```

The Planner might generate:

### Plan A

```text
Save €1,667 per month in cash.
```

### Plan B

```text
Temporarily reduce monthly investment contributions
and redirect the difference into the car fund.
```

### Plan C

```text
Allocate an existing amount into a short-term
low-risk deposit maturing near the purchase date.
```

### Plan D

```text
Use part of current savings immediately and
accumulate the remaining amount monthly.
```

### Plan E

```text
Preserve long-term investments and accept lower
short-term liquidity.
```

Each alternative represents a different trade-off.

---

# 13. Financial Projection

The Planner should not evaluate plans only based on today's balances.

Each plan should produce a projected evolution of the financial state.

Conceptually:

```text
Plan
 |
 v
FinancialProjection
 |
 +-- State at t1
 +-- State at t2
 +-- State at t3
 +-- ...
 +-- State at goal date
```

A projection might track:

```text
Cash
Savings
Investments
Expected returns
Known expenses
Goal funding
Liquidity reserve
Risk indicators
```

---

# 14. Planner

The `Planner` is the main public planning interface.

It acts as a **Facade** over the underlying planning subsystem.

A possible interface might eventually resemble:

```cpp
class Planner {
public:
    std::vector<Plan> propose(
        const FinancialSnapshot& state,
        const Goal& goal,
        const PlannerConfig& config
    ) const;
};
```

The Planner coordinates:

```text
Goal interpretation
Candidate plan generation
Constraint checking
Risk assessment
Optimization
Ranking
Projection
```

It does not execute actions.

It does not mutate the real state.

---

# 15. Facade Pattern

The `Planner` is a natural use of the **Facade Pattern**.

Without a Facade, a GUI or client might need to directly coordinate:

```text
RiskManager
Optimizer
Solver
FundingStrategy
Constraints
ProjectionEngine
```

Instead, the application can expose:

```cpp
planner.propose(...)
```

The Facade provides a simple interface over a more complex subsystem.

The academic justification is:

> The Planner hides orchestration complexity from the application layer while preserving separation between specialized internal components.

---

# 16. Strategy Pattern

Strategy is expected to be one of the central design patterns in the project.

However, there should not be one giant generic Strategy hierarchy.

Different domain questions represent independent variation axes.

Likely strategy categories include:

```text
FundingStrategy
RiskStrategy
OptimizationPolicy
ExecutionStrategy
```

---

# 17. FundingStrategy

A FundingStrategy answers:

> Where should the money required for a goal or expense come from?

Possible strategies include:

```text
PreserveInvestments
PreserveSavings
PreserveLiquidity
BalancedFunding
MinimizeOpportunityCost
```

Example:

For a €10,000 future expense:

### PreserveInvestments

Prefer:

```text
Cash
Savings
Reduced discretionary spending
```

before reducing investment exposure.

### PreserveSavings

Prefer:

```text
Reduced investment contributions
Investment liquidation
```

before using protected savings.

### PreserveLiquidity

Prefer locking or investing current funds if future cash flows can replenish liquidity safely.

---

# 18. RiskStrategy

`RiskStrategy` evaluates the risk of a proposed plan.

Examples of possible risk dimensions include:

```text
Liquidity risk
Investment volatility
Goal failure probability
Concentration risk
Time-horizon mismatch
Emergency reserve risk
```

A potential concept:

```cpp
template<typename T>
concept RiskStrategy =
    requires(
        const T& strategy,
        const FinancialSnapshot& state,
        const Plan& plan
    )
{
    {
        strategy.evaluate(state, plan)
    } -> std::same_as<RiskAssessment>;
};
```

Possible implementations:

```text
ConservativeRisk
BalancedRisk
AggressiveRisk
```

---

# 19. RiskManager

The RiskManager coordinates risk evaluation.

Conceptually:

```text
RiskManager
    |
    v
RiskStrategy
```

A possible static-polymorphism version is:

```cpp
template<RiskStrategy R>
class RiskManager {
public:
    explicit RiskManager(R strategy)
        : strategy_{std::move(strategy)} {}

    RiskAssessment evaluate(
        const FinancialSnapshot& state,
        const Plan& plan
    ) const
    {
        return strategy_.evaluate(state, plan);
    }

private:
    R strategy_;
};
```

---

# 20. Optimizer

The Optimizer determines how plans should be searched and ranked.

It should not be treated as a single monolithic algorithm.

The design should distinguish:

```text
What should be optimized?
```

from:

```text
How should the optimization problem be solved?
```

Therefore:

```text
Optimizer
 |
 +-- OptimizationPolicy
 |
 +-- Solver
```

---

# 21. OptimizationPolicy

An OptimizationPolicy defines the objective or preference structure.

Possible policies include:

```text
PreserveInvestmentsPolicy
PreserveLiquidityPolicy
BalancedPolicy
MaximumExpectedWealthPolicy
MinimizeGoalFailurePolicy
MinimizeOpportunityCostPolicy
```

The policy may define weights or ordering rules for metrics such as:

```text
Expected final wealth
Liquidity
Investment continuity
Goal probability
Risk
Cash reserve
Opportunity cost
```

---

# 22. Solver

The Solver determines how the optimization problem is searched.

Possible solver implementations include:

```text
ExactSolver
GreedySolver
HeuristicSolver
LinearProgrammingSolver
MonteCarloSolver
```

Not all solvers need to be implemented in the first version.

A key conceptual correction is:

> "Optimal" is not itself a solver type.

Optimality is defined relative to an objective function and constraints.

For example:

```text
BalancedPolicy + ExactSolver
BalancedPolicy + HeuristicSolver
PreserveInvestmentsPolicy + ExactSolver
```

are meaningful combinations.

---

# 23. Constraints, Preferences, and Risk

These concepts must remain distinct.

## Constraint

A constraint must be satisfied.

Example:

```text
Available liquid cash must never fall below €5,000.
```

If a plan violates this condition, the plan is invalid.

---

## Preference

A preference affects ranking but does not necessarily invalidate a plan.

Example:

```text
Prefer not to reduce ETF contributions.
```

A plan may violate this preference and still remain valid.

---

## Risk

Risk expresses uncertainty or exposure.

Example:

```text
There is a 30% projected probability of insufficient liquidity.
```

Therefore:

```text
Constraint = must be satisfied
Preference = influences ranking
Risk       = evaluates uncertainty
```

This distinction is central to the architecture.

---

# 24. Liquidity Reserve

The user can configure a minimum readily available amount.

For example:

```text
Minimum liquidity reserve = €5,000
```

This is normally best represented as a hard constraint:

```cpp
struct MinimumLiquidityConstraint {
    Money minimum;
};
```

A plan that leaves the user with only €3,000 liquid is invalid if the configured minimum is €5,000.

---

# 25. Fixed-Term Deposit Example

Assume:

```text
Car price:             €10,000
Purchase date:         24 months
Deposit return:        5%
Minimum cash reserve:  €5,000
Current cash:          €15,000
```

Possible plans may include:

### Option A — Cash reservation

Reserve the entire €10,000 now.

Advantages:

```text
Very low goal risk
Simple
Predictable
```

Disadvantages:

```text
High opportunity cost
Reduced liquidity
```

---

### Option B — Fixed-term deposit

Allocate an amount below €10,000 today into a fixed-term deposit expected to mature at approximately €10,000.

The exact required initial amount depends on:

```text
Interest rate
Compounding
Fees
Taxation assumptions
Deposit duration
```

Advantages:

```text
Capital assigned directly to goal
Potentially lower required initial amount
Low volatility if deposit terms are guaranteed
```

Disadvantages:

```text
Reduced liquidity
Funds may be inaccessible before maturity
```

---

### Option C — Monthly saving

Accumulate the required amount monthly.

Advantages:

```text
Preserves more current liquidity
Simple recurring behaviour
```

Disadvantages:

```text
Higher dependence on future income
Potentially higher goal failure risk
```

---

### Option D — Hybrid

Combine:

```text
Existing savings
Monthly savings
Reduced investment contributions
```

This may create a better balance between liquidity and investment continuity.

---

# 26. Command Pattern

The Planner should not directly perform actions.

Instead, accepted plans are converted into commands.

Examples:

```text
TransferCommand
BuyCommand
SellCommand
CreateDepositCommand
ScheduleSavingCommand
ChangeRecurringInvestmentCommand
```

A possible representation is:

```cpp
using Command = std::variant<
    TransferCommand,
    BuyCommand,
    SellCommand,
    CreateDepositCommand,
    ScheduleSavingCommand
>;
```

A Plan can contain:

```cpp
struct ScheduledCommand {
    Date execution_date;
    Command command;
};
```

---

# 27. Command, Order, Trade, and Transaction

These concepts must remain separate.

```text
Command
    = requested action

Order
    = instruction created for market execution

Trade
    = actual market execution result

Transaction
    = financial event recorded in the application's state
```

For example:

```text
SellCommand
     |
     v
Sell Order
     |
     v
Broker
     |
     v
1..N Trades
     |
     v
Recorded Investment Transaction
```

An order and a trade may not be one-to-one.

A single order may produce:

```text
zero trades
one trade
multiple partial fills
```

---

# 28. Investment Execution Flow

The conceptual investment flow becomes:

```text
InvestmentCommand
        |
        v
      Order
        |
        v
Broker / ExecutionStrategy
        |
        v
      Trade
        |
        v
Recorded Transaction
        |
        v
 FinancialState
```

This is more precise than simply modelling:

```text
Investment -> Order -> Trade
```

because it distinguishes intention, execution, and accounting.

---

# 29. Engine

The Engine is responsible for execution.

It should deliberately remain relatively simple.

The Engine should not decide which financial action is preferable.

That is Planner/Optimizer responsibility.

The Engine may perform:

```text
Command validation
Execution
Broker interaction
Transaction generation
Trade recording
Atomic state mutation
Persistence
Execution event emission
```

A possible conceptual interface is:

```cpp
class Engine {
public:
    ExecutionResult execute(const Command& command);
};
```

The implementation may depend on:

```text
FinancialRepository
Broker
ExecutionStrategy
EventPublisher
```

---

# 30. Engine Invariant

The strongest execution invariant is:

> The Engine is the only application component allowed to mutate the authoritative financial state.

The Planner may simulate changes against copies or snapshots, but never commit them.

This improves:

```text
Correctness
Auditability
Testing
Separation of concerns
Future persistence support
```

---

# 31. Lightweight CQRS-Like Separation

The architecture resembles a lightweight form of CQRS.

Planning/query path:

```text
FinancialSnapshot
      |
      v
Planner
      |
      v
Plans
```

Mutation path:

```text
Command
   |
   v
Engine
   |
   v
FinancialState
```

This does not require implementing a full CQRS framework.

The useful principle is simply:

> Reads and hypothetical planning are separated from authoritative state mutation.

---

# 32. Repository Pattern

Persistence should not be embedded directly in domain objects.

A repository abstraction can manage state access.

Conceptually:

```cpp
class FinancialRepository {
public:
    FinancialSnapshot snapshot() const;
    void apply(const Transaction&);
};
```

The actual interface may later evolve to support atomic groups of transactions.

Potential implementations:

```text
InMemoryFinancialRepository
JsonFinancialRepository
SQLiteFinancialRepository
```

The domain and Planner should not depend on the chosen persistence technology.

---

# 33. Adapter Pattern

Future integration with external systems such as Interactive Brokers should use an Adapter.

Internal code should operate against a domain-level broker abstraction.

Example:

```cpp
class IBroker {
public:
    virtual ~IBroker() = default;

    virtual AccountData fetch_account() = 0;
    virtual ExecutionResult execute(const Order&) = 0;
};
```

Then:

```text
IBroker
 |
 +-- PaperBroker
 |
 +-- IBKRAdapter
 |
 +-- FutureBrokerAdapter
```

The adapter translates between:

```text
Internal Order model
```

and:

```text
External broker API model
```

The internal domain should never depend directly on IBKR-specific data structures.

---

# 34. Manual Input and Future External Data

The initial application will use mostly manually entered financial data.

However, the architecture should make future data sources replaceable.

For example:

```text
AccountDataProvider
 |
 +-- ManualDataProvider
 +-- IBKRDataProvider
 +-- BankAdapter
```

The GUI and Planner should not care whether an account balance came from:

```text
Manual input
JSON
SQLite
IBKR
Bank API
```

---

# 35. Static Polymorphism

The project should prefer static polymorphism where:

```text
the set of implementations is closed;
the implementations are under project control;
runtime extension is not required.
```

C++23 concepts are a good fit.

Example:

```cpp
template<typename T>
concept RiskStrategy =
    requires(
        const T& strategy,
        const FinancialSnapshot& state,
        const Plan& plan
    )
{
    {
        strategy.evaluate(state, plan)
    } -> std::same_as<RiskAssessment>;
};
```

Benefits include:

```text
Compile-time interface checking
No mandatory virtual dispatch
Value semantics
Potential optimization
Clear generic contracts
```

---

# 36. Runtime Selection with `std::variant`

The GUI must allow the user to change strategies at runtime.

This creates an interesting problem:

```text
We want runtime selection
but we also prefer static concrete strategy types.
```

For a closed set of strategies, `std::variant` provides a strong compromise.

Example:

```cpp
using RuntimeRiskStrategy = std::variant<
    ConservativeRisk,
    BalancedRisk,
    AggressiveRisk
>;
```

Dispatch can use:

```cpp
std::visit(...)
```

This provides:

```text
Runtime selection
Value semantics
Closed strategy set
No mandatory inheritance hierarchy
```

---

# 37. Dynamic Polymorphism

Dynamic polymorphism remains appropriate at system boundaries where the implementation set is open-ended.

Examples:

```text
Broker APIs
Persistence backends
External services
Plugin-like integrations
```

For example:

```cpp
class IBroker {
public:
    virtual ~IBroker() = default;
    virtual ExecutionResult execute(const Order&) = 0;
};
```

This is appropriate because new brokers may be added without recompiling architectural assumptions about all possible broker types.

The academic project can explicitly compare:

```text
Classic virtual polymorphism
Concept-based static polymorphism
std::variant runtime dispatch
Type erasure
```

and explain why different mechanisms are chosen in different parts of the architecture.

---

# 38. Factory Pattern

Configuration selected through the GUI may need to be translated into concrete strategy or solver objects.

A Factory can isolate this creation logic.

For example:

```text
Config:
Risk profile = Balanced
Solver = Exact
Funding preference = Preserve Investments
```

can be translated into:

```text
BalancedRisk
ExactSolver
PreserveInvestmentsStrategy
```

The Planner should not contain a large switch statement for constructing every possible dependency.

---

# 39. Builder Pattern

A Builder may be useful if Planner configuration becomes complex.

Example:

```cpp
auto config =
    PlannerConfigBuilder{}
        .currency(Currency::EUR)
        .minimum_liquidity(5'000_eur)
        .risk_profile(RiskProfile::Balanced)
        .solver(SolverType::Exact)
        .funding_strategy(FundingType::PreserveInvestments)
        .build();
```

Builder should only be introduced if configuration construction genuinely becomes complex enough to justify it.

---

# 40. Observer Pattern

Observer or a similar event mechanism may be useful for GUI updates.

For example:

```text
Engine executes command
        |
        v
FinancialState changes
        |
        v
Event emitted
        |
        +--> GUI refresh
        +--> Audit log
        +--> Projection invalidation
```

The GUI should not need to poll all components constantly.

The exact implementation may use:

```text
Observer
Event bus
Signals/slots
Callbacks
```

depending on the GUI framework selected later.

---

# 41. State Pattern

The State Pattern may become relevant for order lifecycle.

Example states:

```text
Created
Validated
Submitted
PartiallyFilled
Filled
Cancelled
Rejected
Expired
```

However, classic GoF State is not automatically required.

A modern C++ implementation may instead use:

```text
enum class
std::variant
explicit state machine
```

The project can compare these approaches.

---

# 42. Singleton Should Be Avoided

The project should avoid global Singleton objects such as:

```cpp
Planner::instance();
Engine::instance();
Config::instance();
```

Reasons include:

```text
Hidden dependencies
Difficult testing
Global mutable state
Poor lifecycle control
Tight coupling
```

Prefer explicit dependency injection:

```cpp
Planner{
    optimizer,
    risk_manager,
    configuration
};
```

---

# 43. Configuration

Configuration may include several independent categories.

```text
PlannerConfig
|
+-- Base settings
|   +-- Currency
|   +-- Time horizon
|
+-- Constraints
|   +-- Minimum liquidity
|   +-- Maximum allocation limits
|
+-- Strategies
|   +-- Funding strategy
|   +-- Risk strategy
|   +-- Optimization policy
|
+-- Solver
|
+-- Execution settings
```

This should remain distinct from mutable financial state.

---

# 44. Proposed Module Structure

```text
DOMAIN
|
+-- Money
+-- Currency
+-- Account
+-- Asset
+-- Position
+-- Income
+-- Expense
+-- Saving
+-- Transaction
+-- Order
+-- Trade
+-- Goal
+-- FutureExpense
+-- FinancialState
+-- FinancialSnapshot
+-- FinancialProjection

PLANNING
|
+-- Planner
+-- Plan
+-- ScheduledCommand
+-- FundingStrategy
+-- RiskManager
|   +-- RiskStrategy
|
+-- Optimizer
    +-- OptimizationPolicy
    +-- Solver

EXECUTION
|
+-- Engine
+-- Command
+-- ExecutionStrategy
+-- ExecutionResult

INFRASTRUCTURE
|
+-- FinancialRepository
+-- Broker
|   +-- PaperBroker
|   +-- IBKRAdapter
|
+-- Persistence
+-- Serialization

CONFIGURATION
|
+-- PlannerConfig
+-- Constraints
+-- Preferences
+-- ComponentFactory

APPLICATION / UI
|
+-- GUI
+-- Controllers / ViewModels
+-- User interaction
```

---

# 45. Suggested C++ Project Layout

A possible repository structure is:

```text
financial-planner/
|
+-- CMakeLists.txt
+-- README.md
+-- docs/
|   +-- architecture.md
|   +-- design-patterns.md
|   +-- domain-model.md
|   +-- decisions/
|
+-- include/
|   +-- planner/
|       +-- domain/
|       +-- planning/
|       +-- execution/
|       +-- infrastructure/
|       +-- configuration/
|
+-- src/
|   +-- domain/
|   +-- planning/
|   +-- execution/
|   +-- infrastructure/
|   +-- configuration/
|
+-- tests/
|   +-- unit/
|   +-- integration/
|   +-- scenarios/
|
+-- apps/
|   +-- cli/
|   +-- gui/
|
+-- examples/
|
+-- external/
```

The final structure should remain pragmatic rather than excessively layered.

---

# 46. Testing Strategy

The architecture should be designed for deterministic testing.

## 46.1 Domain Unit Tests

Examples:

```text
Money arithmetic
Currency mismatch
Priority ordering
Goal validation
Interest calculations
Date calculations
```

---

## 46.2 Strategy Tests

Each Strategy should be testable independently.

For example:

```text
PreserveInvestmentsStrategy
```

should be tested to confirm that it prefers available savings before reducing investment exposure under the relevant conditions.

---

## 46.3 Solver Tests

A Solver should be tested against known optimization cases.

For small problem instances, expected exact solutions can be manually calculated.

---

## 46.4 Planner Scenario Tests

Scenario tests are especially important.

Example:

```text
Current cash: €15,000
Minimum reserve: €5,000
Monthly income: €4,000
Monthly expenses: €2,500
Car goal: €10,000 in 6 months
```

Expected behaviour:

```text
Planner generates multiple valid alternatives.
No valid plan violates the €5,000 minimum reserve.
Plans differ according to configured preferences.
```

---

## 46.5 Engine Tests

The Engine should be tested for:

```text
Correct command validation
State mutation
Transaction creation
Atomic execution
Failure rollback
Broker errors
Partial fills
```

---

## 46.6 Mock External Services

External broker APIs should be replaceable by mocks or fake implementations.

For example:

```text
FakeBroker
PaperBroker
```

This allows deterministic tests without network access or real accounts.

---

# 47. Important Integration Scenario

One of the principal end-to-end scenarios should be the future-car example.

Input:

```text
Current cash
Current investments
Recurring income
Recurring expenses
Minimum liquidity reserve
Car cost
Car purchase date
Risk preference
Optimization preference
Available deposit rates
```

Processing:

```text
Generate candidate funding approaches
Project future states
Reject constraint violations
Evaluate risk
Optimize/rank alternatives
Present alternatives
```

Execution:

```text
User selects plan
Planner produces commands
Engine executes commands
Financial state is updated
```

This scenario exercises almost every major subsystem.

---

# 48. GUI Considerations

The GUI should allow runtime configuration of:

```text
Risk strategy
Funding strategy
Optimization preference
Solver
Liquidity reserve
Goals
Expected future expenses
Income
Expenses
Investment assumptions
```

Because many strategy selections occur at runtime, the architecture should allow runtime configuration without forcing all internal strategy implementations to use inheritance.

This is one reason `std::variant` is particularly relevant.

---

# 49. Possible GUI Workflow

```text
1. User enters financial state
2. User configures constraints
3. User configures strategy preferences
4. User creates a goal
5. Planner generates plans
6. GUI displays alternatives
7. User compares:
   - projected wealth
   - liquidity
   - risk
   - goal probability
   - impact on investments
8. User selects plan
9. Commands are generated
10. Engine executes
11. GUI refreshes state
```

---

# 50. Future IBKR Integration

Interactive Brokers integration should initially be treated as infrastructure, not domain logic.

Potential future capabilities include:

```text
Fetch investment positions
Fetch balances
Fetch market prices
Submit orders
Monitor order status
Record trades
```

The domain should continue to operate on its own types.

Example translation:

```text
Domain BuyOrder
      |
      v
IBKRAdapter
      |
      v
IBKR-specific request
```

and:

```text
IBKR execution response
      |
      v
IBKRAdapter
      |
      v
Domain Trade
```

This prevents external APIs from contaminating the core model.

---

# 51. Design Pattern Summary

| Pattern / Technique | Intended Role |
|---|---|
| Strategy | Risk, funding, optimization preference, execution |
| Command | Represent actions selected for execution |
| Facade | Planner public API |
| Adapter | IBKR and future external integrations |
| Repository | State persistence abstraction |
| Factory | Convert configuration into concrete components |
| Builder | Construct complex configuration if needed |
| Observer | GUI/state notifications |
| State | Potential order lifecycle modelling |
| Concepts | Static polymorphism |
| `std::variant` | Runtime choice among closed alternatives |
| Dependency Injection | Explicit component composition |

The core patterns for the academic discussion should likely be:

```text
Strategy
Command
Facade
Adapter
Repository
```

Supporting patterns can be introduced where justified.

---

# 52. Architectural Decisions to Highlight Academically

## 52.1 Planning and Execution Are Separate

The Planner never directly changes money.

This improves safety and testability.

---

## 52.2 Goals Are Requirements, Not Transactions

A goal expresses a desired future state.

A transaction records an actual financial event.

---

## 52.3 Plans Are Explicit Objects

The system does not immediately collapse reasoning into a single answer.

It retains alternative plans as inspectable domain objects.

---

## 52.4 Constraints and Preferences Are Different

Hard financial rules should not be represented merely as scoring penalties.

---

## 52.5 Optimization Objective and Solver Are Different

The question:

```text
What does the user prefer?
```

is distinct from:

```text
Which algorithm searches the solution space?
```

---

## 52.6 Static and Dynamic Polymorphism Coexist

Static polymorphism is preferred for closed internal policy sets.

Dynamic polymorphism is acceptable at open external boundaries.

---

## 52.7 External Systems Are Adapted

The core domain should remain independent of IBKR, databases, or GUI frameworks.

---

# 53. Potential C++23 Features

The project may make meaningful use of:

```text
Concepts
std::variant
std::visit
std::expected
std::chrono
Ranges
Strong value types
constexpr where appropriate
Designated initializers
Structured bindings
Three-way comparison
Modules, if toolchain support is satisfactory
```

---

# 54. `std::expected`

C++23 `std::expected` can be useful for operations that may fail without using exceptions for ordinary domain errors.

Examples:

```cpp
std::expected<ExecutionResult, ExecutionError>
execute(const Command&);
```

or:

```cpp
std::expected<Money, CurrencyError>
convert(...);
```

Potential errors include:

```text
Insufficient funds
Constraint violation
Invalid date
Unsupported currency
Broker rejection
Persistence failure
```

---

# 55. Domain Errors Versus Programming Errors

The project should distinguish:

```text
Expected domain failure
```

from:

```text
Programming invariant violation
```

For example:

```text
Insufficient funds
```

is a normal domain error.

An impossible internal state caused by a bug may justify an assertion or stronger failure mechanism.

---

# 56. Auditability

Because financial state changes are important, executions should ideally be auditable.

An Engine execution may produce:

```text
Command ID
Timestamp
Command
Result
Generated transactions
Generated trades
Previous state reference
New state reference
```

This can support:

```text
Debugging
Testing
History
Undo-like future features
Reproducibility
```

---

# 57. Future Event-Sourcing Possibility

The project does not initially need full event sourcing.

However, the distinction between:

```text
Command
Transaction
FinancialState
```

makes future event-oriented persistence possible.

A later architecture could rebuild state from recorded financial events.

This is a possible extension, not an initial requirement.

---

# 58. Non-Goals for the First Version

The initial version should avoid unnecessary complexity.

Likely non-goals include:

```text
Real-money automatic trading
High-frequency trading
Tax optimization
Multi-user cloud synchronization
Distributed systems
Machine learning
Cryptocurrency integration
Full event sourcing
Microservices
Plugin architecture
```

These can be reconsidered only if the project develops a genuine need.

---

# 59. Development Roadmap

## Phase 1 — Domain Foundation

Implement:

```text
Money
Currency
Date/time utilities
Account
FinancialState
FinancialSnapshot
Transaction
Goal
FutureExpense
```

Focus on correctness and value semantics.

---

## Phase 2 — Basic Planning

Implement:

```text
Plan
FinancialProjection
Simple constraints
Minimum liquidity reserve
Basic funding strategies
```

Start with deterministic scenarios.

---

## Phase 3 — Optimization

Implement:

```text
Optimizer
OptimizationPolicy
Simple Solver
Plan scoring
```

Begin with a simple exact or brute-force solver for small search spaces if useful.

---

## Phase 4 — Risk

Implement:

```text
RiskAssessment
RiskManager
RiskStrategy
```

Initially use deterministic risk rules before introducing more sophisticated probability models.

---

## Phase 5 — Command and Engine

Implement:

```text
Command
ScheduledCommand
Engine
Transaction generation
FinancialRepository
```

Make Engine the only mutation path.

---

## Phase 6 — Investments

Implement:

```text
Asset
Position
Order
Trade
PaperBroker
ExecutionStrategy
```

Use a virtual paper broker before connecting to any external API.

---

## Phase 7 — GUI

Expose:

```text
State editing
Goal creation
Configuration
Plan comparison
Plan selection
Execution
```

---

## Phase 8 — External Adapter

Implement:

```text
IBroker
IBKRAdapter
```

Initially support read-only functionality before considering order submission.

---

# 60. Documentation Strategy

The project should include documentation beyond code comments.

Recommended documents:

```text
README.md
architecture.md
domain-model.md
design-patterns.md
testing.md
```

For significant decisions, Architecture Decision Records can be useful.

Example:

```text
docs/decisions/001-goals-are-not-transactions.md
docs/decisions/002-engine-exclusive-state-mutation.md
docs/decisions/003-variant-for-runtime-strategies.md
```

---

# 61. CV / Portfolio Value

The project can demonstrate more than simple implementation ability.

A strong project description could emphasize:

> Designed and implemented a modern C++23 financial-planning engine using value-oriented domain modelling, Strategy, Command, Facade, Adapter, and Repository patterns. The system separates hypothetical planning from authoritative execution, supports configurable optimization and risk policies, uses concepts and `std::variant` for static/value-based polymorphism, and is structured for future integration with external broker APIs.

The strongest technical discussion points for interviews include:

```text
Why Goals are not Transactions
Why Planner cannot mutate state
Why Strategy is split into independent policy families
Why optimization objective and solver are separate
Why std::variant is used for closed runtime choices
Why virtual interfaces are retained at external boundaries
Why Command, Order, Trade, and Transaction are separate
Why constraints are distinct from preferences
```

---

# 62. Central Architecture Summary

The final conceptual model is:

```text
                           GUI
                            |
                            v
                         Planner
                            |
              +-------------+-------------+
              |                           |
              v                           v
          Optimizer                   RiskManager
              |                           |
       +------+-------+                   |
       |              |                   |
       v              v                   v
OptimizationPolicy   Solver           RiskStrategy
              |
              v
        Candidate Plans
              |
              v
         Ranked Plans
              |
              v
             GUI
              |
      user selects one
              |
              v
           Commands
              |
              v
            Engine
              |
      +-------+--------+
      |                |
      v                v
    Broker       FinancialRepository
      |                |
      v                v
    Trades       FinancialState
```

The most important conceptual sentence is:

> **The Planner decides what could be done, the Optimizer decides which alternatives are preferable, the RiskManager evaluates whether they are acceptable, the user chooses, and the Engine executes. Only the Engine changes the real financial state.**

---

# 63. Current Open Design Questions

The architecture is intentionally not frozen yet.

The following topics should be decided during implementation:

1. whether Transactions use `std::variant`, templates, or inheritance;
2. whether Goals should form one general type or several specialized types;
3. how FinancialProjection should represent uncertainty;
4. how Plan scoring should be expressed;
5. which first Solver should be implemented;
6. which risk metrics belong in the first version;
7. whether FundingStrategy generates candidates or only ranks funding sources;
8. how recurring commands are represented;
9. how atomic multi-command execution should work;
10. how to organize additional projections in the browser dashboard;
11. how the SQLite ledger should evolve as the domain grows;
12. when IBKR integration should move from read-only to execution support.

These questions should be resolved through use cases and tests rather than by attempting to finalize every abstraction in advance.

---

# 64. Guiding Principles

The project should follow these principles:

```text
Model the domain before choosing patterns.

Use patterns where variation or coupling genuinely requires them.

Prefer value semantics.

Prefer static polymorphism for closed internal strategy sets.

Use runtime polymorphism at open system boundaries.

Keep external APIs outside the core domain.

Separate planning from execution.

Keep authoritative mutation centralized in the Engine.

Treat goals as future requirements.

Treat plans as explicit alternatives.

Distinguish constraints, preferences, and risk.

Make important architectural decisions testable.

Avoid premature generalization.
```

These principles should guide future design decisions as the Financial Planner evolves.
