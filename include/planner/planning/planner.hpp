#pragma once

#include <chrono>
#include <expected>
#include <vector>

#include "planner/domain/financial_snapshot.hpp"
#include "planner/domain/future_expense.hpp"
#include "planner/domain/money.hpp"

namespace planner {

enum class FundingApproach { upfront, gradual };

// Balances reflect all scheduled activity for this month.
struct ProjectedMonth {
    std::chrono::year_month month;
    Money net_cash_flow;
    Money contribution;
    Money purchase;
    Money cash_balance;
    Money earmarked_cash;

    [[nodiscard]] Money available_cash() const { return cash_balance - earmarked_cash; }
};

struct Plan {
    FundingApproach approach;
    std::vector<ProjectedMonth> projection;
    Money lowest_available_cash;
};

struct PlanningResult {
    std::vector<Plan> plans;
    Money shortfall; // Additional starting cash needed when no plan is feasible.
};

enum class PlanningError {
    invalid_month,
    expense_before_snapshot,
    non_positive_expense,
    negative_reserve,
    arithmetic_overflow,
};

class Planner {
public:
    // Cash flow starts in the month after the snapshot and includes the due month.
    [[nodiscard]] std::expected<PlanningResult, PlanningError> propose(
        const FinancialSnapshot& snapshot,
        const FutureExpense& expense,
        Money minimum_reserve) const;
};

} // namespace planner
