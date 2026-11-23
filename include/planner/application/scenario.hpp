#pragma once

#include <chrono>

#include "planner/domain/financial_snapshot.hpp"
#include "planner/domain/future_expense.hpp"

namespace planner {

struct Scenario {
    FinancialSnapshot snapshot;
    FutureExpense expense;
    Money reserve;
};

inline constexpr Scenario car_purchase_example{
    .snapshot = {
        .as_of_month = std::chrono::year{2026} / std::chrono::November,
        .cash = Money{1'500'000},
        .monthly_net_cash_flow = Money{150'000},
    },
    .expense = {
        .due_month = std::chrono::year{2027} / std::chrono::May,
        .amount = Money{1'000'000},
    },
    .reserve = Money{500'000},
};

} // namespace planner
