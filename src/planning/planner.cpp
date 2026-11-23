#include "planner/planning/planner.hpp"

#include <algorithm>
#include <cstddef>
#include <stdexcept>
#include <utility>

namespace planner {
namespace {

Money gradual_initial_contribution(
    Money target, Money monthly_surplus, std::chrono::months horizon) {
    const auto payment_count = horizon.count();
    if (payment_count == 0) {
        return target;
    }

    // Cap the future contributions at the target before multiplying.
    if (monthly_surplus.cents() > target.cents() / payment_count) {
        return Money{};
    }
    return target - Money{monthly_surplus.cents() * payment_count};
}

Plan project_plan(
    const FinancialSnapshot& snapshot,
    const FutureExpense& expense,
    FundingApproach approach,
    Money initial_contribution) {
    const auto horizon = expense.due_month - snapshot.as_of_month;
    const Money monthly_surplus = std::max(Money{}, snapshot.monthly_net_cash_flow);
    Money cash = snapshot.cash;
    Money earmarked_cash;
    Plan plan{.approach = approach, .projection = {}, .lowest_available_cash = cash};
    plan.projection.reserve(static_cast<std::size_t>(horizon.count()) + 1);

    for (std::chrono::months elapsed{0}; elapsed <= horizon; ++elapsed) {
        const bool first_month = elapsed == std::chrono::months{0};
        const Money cash_flow = first_month ? Money{} : snapshot.monthly_net_cash_flow;
        const Money contribution = first_month
            ? initial_contribution
            : std::min(monthly_surplus, expense.amount - earmarked_cash);
        const Money purchase = elapsed == horizon ? expense.amount : Money{};

        cash = cash + cash_flow;
        earmarked_cash = earmarked_cash + contribution;
        cash = cash - purchase;
        earmarked_cash = earmarked_cash - purchase;

        const ProjectedMonth projected{
            .month = snapshot.as_of_month + elapsed,
            .net_cash_flow = cash_flow,
            .contribution = contribution,
            .purchase = purchase,
            .cash_balance = cash,
            .earmarked_cash = earmarked_cash,
        };

        // Earmarking lowers available cash. Spending the earmarked amount preserves it.
        plan.lowest_available_cash = std::min(plan.lowest_available_cash, projected.available_cash());
        plan.projection.push_back(projected);
    }
    return plan;
}

} // namespace

std::expected<PlanningResult, PlanningError> Planner::propose(
    const FinancialSnapshot& snapshot, const FutureExpense& expense, Money minimum_reserve) const try {
    if (!snapshot.as_of_month.ok() || !expense.due_month.ok()) {
        return std::unexpected(PlanningError::invalid_month);
    }
    if (expense.due_month < snapshot.as_of_month) {
        return std::unexpected(PlanningError::expense_before_snapshot);
    }
    if (expense.amount <= Money{}) {
        return std::unexpected(PlanningError::non_positive_expense);
    }
    if (minimum_reserve < Money{}) {
        return std::unexpected(PlanningError::negative_reserve);
    }

    const Money initial_contribution = gradual_initial_contribution(
        expense.amount,
        std::max(Money{}, snapshot.monthly_net_cash_flow),
        expense.due_month - snapshot.as_of_month);
    const bool distinct_schedules = initial_contribution < expense.amount;
    const auto approach = distinct_schedules ? FundingApproach::gradual : FundingApproach::upfront;
    auto gradual = project_plan(snapshot, expense, approach, initial_contribution);

    // Both schedules have the same total cash before the purchase. The gradual
    // schedule earmarks less along the way, so it has the smallest reserve shortfall.
    if (gradual.lowest_available_cash < minimum_reserve) {
        return PlanningResult{.plans = {}, .shortfall = minimum_reserve - gradual.lowest_available_cash};
    }

    PlanningResult result;
    if (distinct_schedules) {
        auto upfront = project_plan(snapshot, expense, FundingApproach::upfront, expense.amount);
        if (upfront.lowest_available_cash >= minimum_reserve) {
            result.plans.push_back(std::move(upfront));
        }
    }
    result.plans.push_back(std::move(gradual));
    return result;
} catch (const std::overflow_error&) {
    return std::unexpected(PlanningError::arithmetic_overflow);
}

} // namespace planner
