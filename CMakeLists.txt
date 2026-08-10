cmake_minimum_required(VERSION 3.21)
project(atrinik-playtester-pathfinding VERSION 0.1.0 LANGUAGES C)

include(FetchContent)
find_package(Python3 3.11 REQUIRED COMPONENTS Interpreter Development.Module)

set(ATRINIK_PATHFINDING_SOURCE_DIR "" CACHE PATH
    "Path to a libatrinik checkout providing the pathfinding source")
if (ATRINIK_PATHFINDING_SOURCE_DIR)
    add_subdirectory("${ATRINIK_PATHFINDING_SOURCE_DIR}/pathfinding"
        "${CMAKE_CURRENT_BINARY_DIR}/pathfinding")
else ()
    FetchContent_Declare(atrinik_pathfinding
        URL "https://github.com/atrinik/legacy-libatrinik/releases/download/v1.1.5/libatrinik-1.1.5.tar.gz"
        URL_HASH "SHA256=3c07b178cc236881f52272df6e38bc2d4186943b782e4cee34b2e733da9c8f9a"
        DOWNLOAD_EXTRACT_TIMESTAMP false
        SOURCE_SUBDIR pathfinding)
    FetchContent_MakeAvailable(atrinik_pathfinding)
endif ()

Python3_add_library(_pathfinding MODULE WITH_SOABI native/pathfinding_module.c)
target_compile_features(_pathfinding PRIVATE c_std_17)
target_link_libraries(_pathfinding PRIVATE Atrinik::Pathfinding)
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
file(COPY "${CMAKE_CURRENT_SOURCE_DIR}/web_static"
    DESTINATION "${ATRINIK_BOT_BUILD_PACKAGE}")
set_target_properties(_pathfinding PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${ATRINIK_BOT_BUILD_PACKAGE}")

install(TARGETS _pathfinding LIBRARY DESTINATION atrinik_bot)
set(ATRINIK_BOT_INSTALL_SOURCES ${ATRINIK_BOT_PYTHON_SOURCES})
list(FILTER ATRINIK_BOT_INSTALL_SOURCES EXCLUDE REGEX "/test_[^/]+\\.py$")
install(FILES ${ATRINIK_BOT_INSTALL_SOURCES}
    "${CMAKE_CURRENT_SOURCE_DIR}/chat_rules.json"
    DESTINATION atrinik_bot)
install(DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/web_static/"
    DESTINATION atrinik_bot/web_static)

include(CTest)
if (BUILD_TESTING)
    add_test(NAME atrinik-bot-pathfinding
        COMMAND "${Python3_EXECUTABLE}" -m unittest -v atrinik_bot.test_pathfinding)
    set_tests_properties(atrinik-bot-pathfinding PROPERTIES
        ENVIRONMENT "PYTHONPATH=${CMAKE_CURRENT_BINARY_DIR}/python")
endif ()
