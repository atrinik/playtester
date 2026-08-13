cmake_minimum_required(VERSION 3.21)
project(atrinik-playtester-pathfinding VERSION 0.1.0 LANGUAGES C)

include(FetchContent)
find_package(Python3 3.11 REQUIRED COMPONENTS Interpreter Development.Module)

file(READ "${CMAKE_CURRENT_SOURCE_DIR}/dependencies.lock.json"
    ATRINIK_DEPENDENCIES_LOCK)
string(JSON ATRINIK_PATHFINDING_RELEASE GET
    "${ATRINIK_DEPENDENCIES_LOCK}" inputs pathfinding release)
string(JSON ATRINIK_PATHFINDING_URL GET
    "${ATRINIK_DEPENDENCIES_LOCK}" inputs pathfinding url)
string(JSON ATRINIK_PATHFINDING_SHA256 GET
    "${ATRINIK_DEPENDENCIES_LOCK}" inputs pathfinding sha256)

FetchContent_Declare(atrinik_pathfinding
    URL "${ATRINIK_PATHFINDING_URL}"
    URL_HASH "SHA256=${ATRINIK_PATHFINDING_SHA256}"
    DOWNLOAD_EXTRACT_TIMESTAMP false
    SOURCE_SUBDIR pathfinding)
FetchContent_MakeAvailable(atrinik_pathfinding)

Python3_add_library(_pathfinding MODULE WITH_SOABI native/pathfinding_module.c)
target_compile_features(_pathfinding PRIVATE c_std_17)
target_link_libraries(_pathfinding PRIVATE Atrinik::Pathfinding)
target_compile_definitions(_pathfinding PRIVATE
    ATRINIK_PATHFINDING_RELEASE="${ATRINIK_PATHFINDING_RELEASE}"
    ATRINIK_PATHFINDING_SHA256="${ATRINIK_PATHFINDING_SHA256}")
if (CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(_pathfinding PRIVATE
        -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror)
endif ()

set(ATRINIK_BOT_BUILD_PACKAGE "${CMAKE_CURRENT_BINARY_DIR}/python/atrinik_bot")
file(MAKE_DIRECTORY "${ATRINIK_BOT_BUILD_PACKAGE}")
file(GLOB ATRINIK_BOT_PYTHON_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/*.py")
foreach (source IN LISTS ATRINIK_BOT_PYTHON_SOURCES)
    get_filename_component(name "${source}" NAME)
    configure_file("${source}" "${ATRINIK_BOT_BUILD_PACKAGE}/${name}" COPYONLY)
endforeach ()
file(COPY "${CMAKE_CURRENT_SOURCE_DIR}/chat_rules.json"
    DESTINATION "${ATRINIK_BOT_BUILD_PACKAGE}")
file(COPY "${CMAKE_CURRENT_SOURCE_DIR}/dependencies.lock.json"
    DESTINATION "${ATRINIK_BOT_BUILD_PACKAGE}")
file(COPY "${CMAKE_CURRENT_SOURCE_DIR}/web_static"
    DESTINATION "${ATRINIK_BOT_BUILD_PACKAGE}")
set_target_properties(_pathfinding PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${ATRINIK_BOT_BUILD_PACKAGE}")

install(TARGETS _pathfinding LIBRARY DESTINATION atrinik_bot)
set(ATRINIK_BOT_INSTALL_SOURCES ${ATRINIK_BOT_PYTHON_SOURCES})
list(FILTER ATRINIK_BOT_INSTALL_SOURCES EXCLUDE REGEX "/test_[^/]+\\.py$")
install(FILES ${ATRINIK_BOT_INSTALL_SOURCES}
    "${CMAKE_CURRENT_SOURCE_DIR}/chat_rules.json"
    "${CMAKE_CURRENT_SOURCE_DIR}/dependencies.lock.json"
    DESTINATION atrinik_bot)
install(DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/web_static/"
    DESTINATION atrinik_bot/web_static)

include(CTest)
if (BUILD_TESTING)
    add_test(NAME atrinik-bot-pathfinding
        COMMAND "${Python3_EXECUTABLE}" -m unittest -v atrinik_bot.test_pathfinding)
    add_test(NAME atrinik-bot-compatibility
        COMMAND "${Python3_EXECUTABLE}" -m unittest -v atrinik_bot.test_compatibility)
    set_tests_properties(atrinik-bot-pathfinding PROPERTIES
        ENVIRONMENT "PYTHONPATH=${CMAKE_CURRENT_BINARY_DIR}/python")
    set_tests_properties(atrinik-bot-compatibility PROPERTIES
        ENVIRONMENT "PYTHONPATH=${CMAKE_CURRENT_BINARY_DIR}/python")
endif ()
