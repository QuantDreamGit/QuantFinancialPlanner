#include "planner/application/scenario.hpp"
#include "planner/planning/planner.hpp"

#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <format>
#include <iostream>
#include <limits>
#include <optional>
#include <string>
#include <string_view>
#include <system_error>

namespace {

using planner::Money;

constexpr std::int64_t cents_per_euro = 100;

std::string format_money(Money amount) {
    const auto cents = amount.cents();
    return std::format("{}EUR {}.{:02}", cents < 0 ? "-" : "",
        std::abs(cents / cents_per_euro), std::abs(cents % cents_per_euro));
}

std::optional<Money> parse_purchase_price(std::string_view argument) {
    std::int64_t euros{};
    const auto end = argument.data() + argument.size();
    const auto [parsed_end, error] = std::from_chars(argument.data(), end, euros);
    if (error != std::errc{} || parsed_end != end || euros <= 0 ||
        euros > std::numeric_limits<std::int64_t>::max() / cents_per_euro) {
        return std::nullopt;
    }
    return Money{euros * cents_per_euro};
}

void print_usage(std::ostream& output) {
    output << "Usage\n  financial_planner [purchase-price-in-euros]\n\n"
        "Run the car purchase demo with a positive whole-euro price. The default is 10000.\n";
}

std::string_view error_message(planner::PlanningError error) {
    using enum planner::PlanningError;
    switch (error) {
        case invalid_month: return "Enter valid calendar months.";
        case expense_before_snapshot: return "The due month precedes the starting balance.";
        case non_positive_expense: return "The purchase price must be positive.";
        case negative_reserve: return "The reserve must be zero or greater.";
        case arithmetic_overflow: return "The projection exceeds the supported range of EUR amounts.";
    }
    return "Planning failed.";
}

void print_plan(const planner::Plan& plan) {
    const auto name = plan.approach == planner::FundingApproach::upfront
        ? "Fund upfront" : "Fund gradually";
    std::cout << '\n' << name << '\n'
        << "Lowest available cash " << format_money(plan.lowest_available_cash) << '\n'
        << std::format("{:<7} {:>15} {:>15} {:>15} {:>15}\n",
            "Month", "Set aside", "Purchase", "Available", "Earmarked");

    for (const auto& month : plan.projection) {
        std::cout << std::format("{:%Y-%m} {:>15} {:>15} {:>15} {:>15}\n",
            month.month, format_money(month.contribution), format_money(month.purchase),
            format_money(month.available_cash()), format_money(month.earmarked_cash));
    }
}

} // namespace

int main(int argc, char* argv[]) {
    if (argc > 2) {
        print_usage(std::cerr);
        return EXIT_FAILURE;
    }

    auto scenario = planner::car_purchase_example;
    if (argc == 2) {
        const std::string_view argument{argv[1]};
        if (argument == "--help") {
            print_usage(std::cout);
            return EXIT_SUCCESS;
        }
        const auto price = parse_purchase_price(argument);
        if (!price) {
            print_usage(std::cerr);
            return EXIT_FAILURE;
        }
        scenario.expense.amount = *price;
    }

    const auto& [snapshot, expense, reserve] = scenario;

    const auto result = planner::Planner{}.propose(snapshot, expense, reserve);
    if (!result) {
        std::cerr << error_message(result.error()) << '\n';
        return EXIT_FAILURE;
    }

    std::cout << std::format("Car purchase demo\n"
        "Starting cash in {:%Y-%m}  {}\n"
        "Monthly net cash flow     {}\n"
        "Minimum liquidity reserve {}\n"
        "Purchase due in {:%Y-%m}   {}\n",
        snapshot.as_of_month, format_money(snapshot.cash),
        format_money(snapshot.monthly_net_cash_flow), format_money(reserve),
        expense.due_month, format_money(expense.amount));

    if (result->plans.empty()) {
        std::cout << "\nNo feasible funding plan. Additional starting cash needed is "
            << format_money(result->shortfall) << ".\n";
    }
    for (const auto& plan : result->plans) {
        print_plan(plan);
    }
    return EXIT_SUCCESS;
}
