# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# A directory lock for a step that must not run twice at once in one checkout, such as
# `npm ci` (which deletes and rewrites node_modules) when two terminals deploy two
# environments. Sourced, not executed:
#
#   . deployment/build-lock.sh
#   build_lock_acquire "$REPO_ROOT/frontend/.build.lock" "frontend build"
#   ...
#   build_lock_release
#
# mkdir is atomic, so exactly one caller creates the directory. The holder's PID is
# written inside it; a lock whose holder has exited (Ctrl-C, a crash) is taken over
# rather than waited on forever. Released on exit as well, so a failing step never
# leaves the lock behind.

BUILD_LOCK_DIR=""
BUILD_LOCK_WAIT_S="${BUILD_LOCK_WAIT_S:-1800}"

build_lock_acquire() {
    local lock="$1" what="${2:-build}" waited=0 holder
    while ! mkdir "$lock" 2>/dev/null; do
        holder="$(cat "$lock/pid" 2>/dev/null || true)"
        if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
            rm -rf "$lock"     # its holder is gone
            continue
        fi
        if [ "$waited" -eq 0 ]; then
            echo "   waiting: another environment is running the $what in this checkout"
        fi
        if [ "$waited" -ge "$BUILD_LOCK_WAIT_S" ]; then
            echo "❌ Waited ${BUILD_LOCK_WAIT_S}s for $lock. If no other deploy is running," >&2
            echo "   remove it: rm -rf '$lock'" >&2
            return 1
        fi
        sleep 2
        waited=$((waited + 2))
    done
    echo "$$" > "$lock/pid"
    BUILD_LOCK_DIR="$lock"
    trap build_lock_release EXIT
}

build_lock_release() {
    if [ -n "$BUILD_LOCK_DIR" ]; then
        rm -rf "$BUILD_LOCK_DIR"
        BUILD_LOCK_DIR=""
    fi
}
