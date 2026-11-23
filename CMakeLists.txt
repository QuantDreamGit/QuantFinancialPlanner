cmake_minimum_required(VERSION 3.20)
project(QuantFinancialPlanner LANGUAGES CXX)

set(CMAKE_CXX_EXTENSIONS OFF)

add_library(planner_core src/planning/planner.cpp)
target_compile_features(planner_core PUBLIC cxx_std_23)
target_include_directories(planner_core PUBLIC include)

add_executable(financial_planner apps/cli/main.cpp)
target_link_libraries(financial_planner PRIVATE planner_core)

add_executable(planner_projection apps/web/projection.cpp)
target_link_libraries(planner_projection PRIVATE planner_core)

include(CTest)
if(BUILD_TESTING)
    add_executable(planner_scenarios tests/scenarios/planner_scenarios.cpp)
    target_link_libraries(planner_scenarios PRIVATE planner_core)
    add_test(NAME planner_scenarios COMMAND planner_scenarios)
    find_package(Python3 3.10 COMPONENTS Interpreter)
    if(Python3_Interpreter_FOUND)
        add_test(
            NAME planner_web_tests
            COMMAND "${CMAKE_COMMAND}" -E env "PLANNER_PROJECTION=$<TARGET_FILE:planner_projection>"
                "${Python3_EXECUTABLE}" -m unittest discover -s tests/web
        )
        set_tests_properties(planner_web_tests PROPERTIES WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}")
    endif()
endif()
