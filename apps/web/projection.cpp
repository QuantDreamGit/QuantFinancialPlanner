#include "planner/application/scenario.hpp"
#include "planner/planning/planner.hpp"

#include <charconv>
#include <chrono>
#include <cstdlib>
#include <format>
#include <iostream>
#include <optional>
#include <string_view>

namespace {

using planner::Money;

std::string month_string(std::chrono::year_month month) {
    return std::format("{:04}-{:02}", int(month.year()), unsigned(month.month()));
}

std::string euro_string(Money amount) {
    const auto cents = amount.cents();
    return std::format("{}{}.{:02}", cents < 0 ? "-" : "",
        std::abs(cents / 100), std::abs(cents % 100));
}

template<typename Integer>
std::optional<Integer> parse_integer(std::string_view input) {
    Integer value{};
    const auto [end, error] = std::from_chars(input.data(), input.data() + input.size(), value);
    if (error != std::errc{} || end != input.data() + input.size()) {
        return std::nullopt;
    }
    return value;
}

std::optional<std::chrono::year_month> parse_month(std::string_view input) {
    if (input.size() != 7 || input[4] != '-') {
        return std::nullopt;
    }
    const auto year = parse_integer<int>(input.substr(0, 4));
    const auto month = parse_integer<unsigned>(input.substr(5));
    if (!year || !month || *year < 1) {
        return std::nullopt;
    }
    const auto value = std::chrono::year{*year} / std::chrono::month{*month};
    return value.ok() ? std::optional{value} : std::nullopt;
}

void write_example() {
    const auto& [snapshot, expense, reserve] = planner::car_purchase_example;
    std::cout << std::format(
        R"({{"startingMonth":"{}","startingCash":"{}","monthlyNetCashFlow":"{}","minimumReserve":"{}","purchasePrice":"{}","dueMonth":"{}"}})",
        month_string(snapshot.as_of_month), euro_string(snapshot.cash),
        euro_string(snapshot.monthly_net_cash_flow), euro_string(reserve),
        euro_string(expense.amount), month_string(expense.due_month));
}

std::string_view error_name(planner::PlanningError error) {
    using enum planner::PlanningError;
    switch (error) {
        case invalid_month: return "invalid_month";
        case expense_before_snapshot: return "expense_before_snapshot";
        case non_positive_expense: return "non_positive_expense";
        case negative_reserve: return "negative_reserve";
        case arithmetic_overflow: return "arithmetic_overflow";
    }
    return "unknown";
}

void write_result(const planner::Scenario& scenario) {
    const auto& [snapshot, expense, reserve] = scenario;
    const auto result = planner::Planner{}.propose(snapshot, expense, reserve);
    if (!result) {
        std::cout << std::format(R"({{"error":"{}"}})", error_name(result.error()));
        return;
    }
    std::cout << std::format(R"({{"reserve":"{}","shortfall":"{}","plans":[)",
        reserve.cents(), result->shortfall.cents());
    const char* plan_separator = "";
    for (const auto& plan : result->plans) {
        std::cout << plan_separator << std::format(
            R"({{"approach":"{}","lowestAvailableCash":"{}","projection":[)",
            plan.approach == planner::FundingApproach::upfront ? "upfront" : "gradual",
            plan.lowest_available_cash.cents());
        const char* month_separator = "";
        for (const auto& row : plan.projection) {
            std::cout << month_separator << std::format(
                R"({{"month":"{}","netCashFlow":"{}","contribution":"{}","purchase":"{}","availableCash":"{}","earmarkedCash":"{}"}})",
                month_string(row.month), row.net_cash_flow.cents(), row.contribution.cents(),
                row.purchase.cents(), row.available_cash().cents(), row.earmarked_cash.cents());
            month_separator = ",";
        }
        std::cout << "]}";
        plan_separator = ",";
    }
    std::cout << "]}";
}

} // namespace

int main(int argc, char* argv[]) {
    if (argc == 2 && std::string_view{argv[1]} == "--example") {
        write_example();
        return EXIT_SUCCESS;
    }
    if (argc != 7) {
        return EXIT_FAILURE;
    }
    const auto start = parse_month(argv[1]);
    const auto cash = parse_integer<std::int64_t>(argv[2]);
    const auto flow = parse_integer<std::int64_t>(argv[3]);
    const auto reserve = parse_integer<std::int64_t>(argv[4]);
    const auto price = parse_integer<std::int64_t>(argv[5]);
    const auto due = parse_month(argv[6]);
    if (!start || !cash || !flow || !reserve || !price || !due) {
        return EXIT_FAILURE;
    }
    write_result({{*start, Money{*cash}, Money{*flow}}, {*due, Money{*price}}, Money{*reserve}});
    return EXIT_SUCCESS;
}
