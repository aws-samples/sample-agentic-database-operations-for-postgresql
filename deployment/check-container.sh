#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Check that a container runtime is installed AND usable, and say what to do when it
# is not.
#
# Two failures look identical from the outside and have different fixes: no runtime
# installed at all, and a runtime installed whose VM is not started. On macOS the
# second is the common one, because finch, colima and podman all run a Linux VM that
# has to be created once and started per boot. "docker: command not found" and
# "Cannot connect to the Docker daemon" are equally unhelpful if you do not already
# know which of those you are looking at, so this separates them and prints the exact
# command for the runtime and OS in front of you.
#
# Usage:  check-container.sh [--quiet]
#         Exit 0 = a runtime is installed and responding.
set -uo pipefail

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1
say() { [ "$QUIET" = "0" ] && printf '%s\n' "$1"; return 0; }

OS="$(uname -s)"
ARCH="$(uname -m)"

# The images are built for arm64, so an x86 host needs emulation. Checked because the
# failure is otherwise an obscure exec-format error deep in a build.
TARGET_ARCH="arm64"

# docker first: if colima or Rancher Desktop is in use, the docker CLI is the front
# end and is what the build scripts will pick up.
RUNTIME=""
for c in docker finch podman nerdctl; do
    if command -v "$c" >/dev/null 2>&1; then RUNTIME="$c"; break; fi
done

recommend_install() {
    case "$OS" in
        Darwin)
            say ""
            say "   Install one of these (both run a small Linux VM; either is fine):"
            say ""
            say "     Finch — AWS's own, and what this project is tested with"
            say "       brew install --cask finch"
            say "       finch vm init"
            say ""
            say "     Colima — closer to a drop-in Docker replacement, brings the docker CLI"
            say "       brew install colima docker"
            say "       colima start --arch aarch64"
            say ""
            say "   Docker Desktop also works if you already have a licence for it."
            ;;
        Linux)
            say ""
            say "   Install one of these:"
            say ""
            say "     Docker Engine   https://docs.docker.com/engine/install/"
            say "                     then add yourself to the docker group and log back in"
            say ""
            say "     Podman          sudo apt install podman   (Debian/Ubuntu)"
            say "                     sudo dnf install podman   (Fedora/RHEL)"
            say ""
            say "   No VM is needed on Linux - these talk to the host kernel directly."
            ;;
        *)
            say ""
            say "   On Windows, run this from WSL2 and install Docker Engine inside it, or"
            say "   use Docker Desktop or Rancher Desktop with the WSL2 backend."
            ;;
    esac
}

# How to start each runtime, which is nearly always the real problem on macOS.
recommend_start() {
    case "$RUNTIME" in
        finch)
            say "      finch vm init     # first time on this machine"
            say "      finch vm start    # after a reboot"
            ;;
        podman)
            if [ "$OS" = "Darwin" ]; then
                say "      podman machine init    # first time on this machine"
                say "      podman machine start   # after a reboot"
            else
                say "      systemctl --user start podman.socket"
            fi
            ;;
        nerdctl)
            say "      Start the containerd host it talks to (lima, Rancher Desktop, ...)."
            ;;
        docker)
            # The docker CLI is a front end for several different back ends. Name the
            # one that is actually installed rather than guessing "start Docker Desktop".
            if command -v colima >/dev/null 2>&1; then
                say "      colima start      # colima is installed and provides your docker CLI"
            elif [ "$OS" = "Darwin" ]; then
                say "      Start Docker Desktop, or if you use colima: colima start"
            else
                say "      sudo systemctl start docker"
            fi
            ;;
    esac
}

if [ -z "$RUNTIME" ]; then
    say "   Container runtime.... ❌ none found (docker, finch, podman or nerdctl)"
    say "                            needed to build the backend and batch images"
    recommend_install
    exit 1
fi

if ! "$RUNTIME" info >/dev/null 2>&1; then
    say "   Container runtime.... ⚠️  $RUNTIME is installed but not responding"
    say "                            it is most likely not started yet:"
    recommend_start
    exit 1
fi

say "   Container runtime.... ✅ $RUNTIME"

# Native on Apple silicon and arm64 Linux; emulated elsewhere. Emulated builds do work
# but are slow, and silently so - worth saying rather than leaving someone to wonder
# why an image build takes twenty minutes.
if [ "$ARCH" != "$TARGET_ARCH" ] && [ "$ARCH" != "aarch64" ]; then
    say "                            note: images are built for linux/$TARGET_ARCH and this"
    say "                            host is $ARCH, so the build runs under emulation and"
    say "                            will be slow. It should still succeed."
fi
exit 0
