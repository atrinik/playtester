# Record the source revision for the dashboard and `--version`.
#
# Run as `cmake -DSOURCE_DIR=... -DOUTPUT=... -P build_info.cmake` on every
# build, so a rebuilt checkout reports its current commit. A tree without Git
# metadata (or without git) records an unknown revision instead of failing.
# The file is rewritten only when its content changes.

find_package(Git QUIET)
set(describe "")
set(commit "")
set(dirty false)
if (GIT_EXECUTABLE)
    # Only this source tree's own repository counts: a copy without .git that
    # sits inside another worktree must not report the enclosing commit.
    execute_process(
        COMMAND "${GIT_EXECUTABLE}" -C "${SOURCE_DIR}" rev-parse --show-toplevel
        OUTPUT_VARIABLE toplevel OUTPUT_STRIP_TRAILING_WHITESPACE
        RESULT_VARIABLE toplevel_result ERROR_QUIET)
    if (toplevel_result EQUAL 0)
        file(REAL_PATH "${toplevel}" toplevel)
        file(REAL_PATH "${SOURCE_DIR}" source_real)
        if (toplevel STREQUAL source_real)
            execute_process(
                COMMAND "${GIT_EXECUTABLE}" -C "${SOURCE_DIR}" rev-parse HEAD
                OUTPUT_VARIABLE commit OUTPUT_STRIP_TRAILING_WHITESPACE
                RESULT_VARIABLE commit_result ERROR_QUIET)
            if (NOT commit_result EQUAL 0)
                set(commit "")
            endif ()
        endif ()
    endif ()
endif ()
if (commit)
    # Semantic-release tags every release commit as vMAJOR.MINOR.PATCH.
    # --always keeps a result (and its -dirty suffix) when no tag is reachable.
    execute_process(
        COMMAND "${GIT_EXECUTABLE}" -C "${SOURCE_DIR}" describe --tags --long
                --match "v[0-9]*" --abbrev=7 --always --dirty
        OUTPUT_VARIABLE describe OUTPUT_STRIP_TRAILING_WHITESPACE
        RESULT_VARIABLE describe_result ERROR_QUIET)
    if (NOT describe_result EQUAL 0)
        set(describe "")
    endif ()
endif ()

set(tag "")
set(distance 0)
if (describe MATCHES "^(.+)-([0-9]+)-g[0-9a-f]+(-dirty)?$")
    set(tag "${CMAKE_MATCH_1}")
    set(distance "${CMAKE_MATCH_2}")
endif ()
# describe refreshes the index first, so build output and stale stat data in
# ignored directories do not count as local changes.
if (describe MATCHES "-dirty$")
    set(dirty true)
endif ()

set(content "{\n  \"tag\": \"${tag}\",\n  \"distance\": ${distance},\n  \"commit\": \"${commit}\",\n  \"dirty\": ${dirty}\n}\n")
set(previous "")
if (EXISTS "${OUTPUT}")
    file(READ "${OUTPUT}" previous)
endif ()
if (NOT previous STREQUAL content)
    file(WRITE "${OUTPUT}" "${content}")
endif ()
