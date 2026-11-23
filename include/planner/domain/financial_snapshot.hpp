#pragma once

#include <chrono>

#include "planner/domain/money.hpp"

namespace planner {

struct FinancialSnapshot {
    std::chrono::year_month as_of_month;
    Money cash;
    Money monthly_net_cash_flow;
};

} // namespace planner
