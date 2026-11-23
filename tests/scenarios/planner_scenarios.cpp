#include "planner/planning/planner.hpp"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <stdexcept>

namespace {

using namespace std::chrono;
using planner::FinancialSnapshot;
using planner::FundingApproach;
using planner::FutureExpense;
using planner::Money;
using planner::Planner;
using planner::PlanningError;

constexpr FinancialSnapshot snapshot{
    .as_of_month = year{2026} / November,
    .cash = Money{1'500'000},
    .monthly_net_cash_flow = Money{150'000},
};
constexpr auto purchase_month = year{2027} / May;
constexpr Money reserve{500'000};

void require(bool condition, const char* message) {
    if (!condition) {
        throw std::runtime_error{message};
    }
}

void compares_two_car_purchase_plans() {
    const FutureExpense expense{purchase_month, Money{1'000'000}};
    const auto result = Planner{}.propose(snapshot, expense, reserve);
    require(result && result->plans.size() == 2, "The car purchase should have two plans");
    require(result->shortfall == Money{}, "Feasible plans should have zero shortfall");

    const auto& upfront = result->plans[0];
    const auto& gradual = result->plans[1];
    require(upfront.approach == FundingApproach::upfront, "Expected the upfront plan first");
    require(gradual.approach == FundingApproach::gradual, "Expected the gradual plan second");
    require(upfront.lowest_available_cash == reserve, "The upfront plan may meet the reserve exactly");
    require(gradual.lowest_available_cash == Money{1'400'000}, "The gradual plan should retain EUR 14000");
    require(upfront.projection.size() == 7 && gradual.projection.size() == 7,
        "Both projections should include the starting month and six later months");

    const auto& upfront_start = upfront.projection.front();
    const auto& gradual_start = gradual.projection.front();
    require(upfront_start.month == snapshot.as_of_month, "The projection should begin at the snapshot");
    require(upfront_start.net_cash_flow == Money{} && gradual_start.net_cash_flow == Money{},
        "The starting balance already includes the starting month");
    require(upfront_start.contribution == Money{1'000'000}, "The upfront plan should earmark EUR 10000 now");
    require(gradual_start.contribution == Money{100'000}, "The gradual plan should earmark EUR 1000 now");

    for (std::size_t index = 1; index < gradual.projection.size(); ++index) {
        const auto& month = gradual.projection[index];
        require(month.net_cash_flow == Money{150'000}, "Expected monthly cash flow through the due month");
        require(month.contribution == Money{150'000}, "Expected six EUR 1500 contributions");
        require(month.available_cash() == Money{1'400'000}, "Earmarked funds count separately");
        require(upfront.projection[index].contribution == Money{}, "Upfront funding needs no later contributions");
    }

    for (const auto& plan : result->plans) {
        const auto& last = plan.projection.back();
        require(last.month == purchase_month, "The timeline should cross the calendar year to the due month");
        require(last.purchase == expense.amount && last.earmarked_cash == Money{},
            "The purchase should spend the goal fund in the due month");
        require(last.cash_balance == Money{1'400'000}, "Both plans should end with EUR 14000 in cash");
    }
}

void filters_plans_and_reports_the_shortfall() {
    const Planner planner;
    const auto gradual_only = planner.propose(snapshot, FutureExpense{purchase_month, Money{1'900'000}}, reserve);
    require(gradual_only && gradual_only->plans.size() == 1,
        "The reserve should filter out the upfront plan for a EUR 19000 purchase");
    require(gradual_only->plans.front().approach == FundingApproach::gradual &&
        gradual_only->plans.front().lowest_available_cash == reserve,
        "Gradual funding is feasible at the EUR 19000 boundary");

    const auto infeasible = planner.propose(snapshot, FutureExpense{purchase_month, Money{2'000'000}}, reserve);
    require(infeasible && infeasible->plans.empty(), "The plan cannot spend EUR 20000 and retain the reserve");
    require(infeasible->shortfall == Money{100'000}, "The EUR 20000 purchase should report a EUR 1000 shortfall");
}

void checks_the_opening_balance_and_later_months() {
    const Planner planner;
    const auto below_reserve = planner.propose(
        snapshot, FutureExpense{purchase_month, Money{700'000}}, Money{1'600'000});
    require(below_reserve && below_reserve->plans.empty() && below_reserve->shortfall == Money{100'000},
        "Future cash cannot satisfy a reserve that is already breached");

    auto monthly_deficit = snapshot;
    monthly_deficit.monthly_net_cash_flow = Money{-50'000};
    const auto drops_below_reserve = planner.propose(
        monthly_deficit, FutureExpense{purchase_month, Money{800'000}}, reserve);
    require(drops_below_reserve && drops_below_reserve->plans.empty() &&
        drops_below_reserve->shortfall == Money{100'000},
        "A monthly deficit can breach the reserve after funding upfront");
}

void handles_immediate_and_small_purchases() {
    const Planner planner;
    const auto immediate = planner.propose(
        snapshot, FutureExpense{snapshot.as_of_month, Money{1'000'000}}, reserve);
    require(immediate && immediate->plans.size() == 1 && immediate->plans.front().projection.size() == 1,
        "An immediate purchase should have a single upfront schedule");
    require(immediate->plans.front().projection.front().available_cash() == reserve,
        "An immediate purchase spends from the starting balance");

    const auto small = planner.propose(snapshot, FutureExpense{purchase_month, Money{150'001}}, reserve);
    require(small && small->plans.size() == 2, "A small later purchase should still have both schedules");
    const auto& projection = small->plans.back().projection;
    require(projection.size() == 7, "A small purchase is still paid in the due month");
    require(projection[0].contribution == Money{} && projection[1].contribution == Money{150'000} &&
        projection[2].contribution == Money{1} && projection[3].contribution == Money{},
        "Gradual funding should stop at the exact target in cents");
}

void validates_inputs_and_money_range() {
    const Planner planner;
    const FutureExpense expense{purchase_month, Money{1'000'000}};
    const auto invalid_month = planner.propose(snapshot, FutureExpense{year{2027} / month{13}, expense.amount}, reserve);
    const auto past_due = planner.propose(snapshot, FutureExpense{year{2026} / October, expense.amount}, reserve);
    const auto zero_cost = planner.propose(snapshot, FutureExpense{purchase_month, Money{}}, reserve);
    const auto negative_reserve = planner.propose(snapshot, expense, Money{-1});
    require(!invalid_month && invalid_month.error() == PlanningError::invalid_month, "Reject invalid calendar months");
    require(!past_due && past_due.error() == PlanningError::expense_before_snapshot, "Reject dates before the snapshot");
    require(!zero_cost && zero_cost.error() == PlanningError::non_positive_expense, "Reject a zero purchase price");
    require(!negative_reserve && negative_reserve.error() == PlanningError::negative_reserve, "Reject negative reserves");

    auto large_balance = snapshot;
    large_balance.cash = Money{std::numeric_limits<std::int64_t>::max()};
    const auto overflow = planner.propose(large_balance, expense, reserve);
    require(!overflow && overflow.error() == PlanningError::arithmetic_overflow, "Detect cash balance overflow");
}

} // namespace

int main() try {
    compares_two_car_purchase_plans();
    filters_plans_and_reports_the_shortfall();
    checks_the_opening_balance_and_later_months();
    handles_immediate_and_small_purchases();
    validates_inputs_and_money_range();
    return EXIT_SUCCESS;
} catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return EXIT_FAILURE;
}
