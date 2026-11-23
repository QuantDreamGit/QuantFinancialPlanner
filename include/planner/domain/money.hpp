#pragma once

#include <compare>
#include <cstdint>
#include <limits>
#include <stdexcept>

namespace planner {

// A EUR amount represented as signed cents.
class Money {
public:
    constexpr Money() = default;
    constexpr explicit Money(std::int64_t cents) noexcept : cents_{cents} {}

    [[nodiscard]] constexpr std::int64_t cents() const noexcept { return cents_; }
    constexpr auto operator<=>(const Money&) const noexcept = default;

    friend constexpr Money operator+(Money left, Money right) {
        using Limits = std::numeric_limits<std::int64_t>;
        const auto cents = right.cents_;
        if ((cents > 0 && left.cents_ > Limits::max() - cents) ||
            (cents < 0 && left.cents_ < Limits::min() - cents)) {
            throw std::overflow_error{"Money addition exceeds the range of integer cents"};
        }
        return Money{left.cents_ + cents};
    }

    friend constexpr Money operator-(Money left, Money right) {
        using Limits = std::numeric_limits<std::int64_t>;
        const auto cents = right.cents_;
        if ((cents > 0 && left.cents_ < Limits::min() + cents) ||
            (cents < 0 && left.cents_ > Limits::max() + cents)) {
            throw std::overflow_error{"Money subtraction exceeds the range of integer cents"};
        }
        return Money{left.cents_ - cents};
    }

private:
    std::int64_t cents_{};
};

} // namespace planner
