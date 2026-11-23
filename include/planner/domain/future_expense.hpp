#pragma once

#include <chrono>

#include "planner/domain/money.hpp"

namespace planner {

struct FutureExpense {
    std::chrono::year_month due_month;
    Money amount;
};

} // namespace planner
