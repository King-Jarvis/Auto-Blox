#!/usr/bin/env bash
# Install or update Auto-Blox on an Orange Pi Zero 2W, in one command:
#
#   curl -fsSL https://raw.githubusercontent.com/King-Jarvis/auto-blox/main/get.sh | bash
#   curl -fsSL .../get.sh | bash -s -- --iot --mpy      # options go to install.sh
#   bash get.sh --download-only                          # fetch or update, install nothing
#
# Run it as the user the console should run as, not as root: it asks sudo for
# the install step itself. It puts the code in ~/auto-blox (AUTOBLOX_DIR to
# change that) and runs scripts/install.sh from there. Run it again to update:
# a git checkout is pulled; a downloaded copy is replaced, keeping backups/.
# Everything the console stores lives in ~/.config/auto-blox, not here.
#
# Everything is inside main(), called on the last line, so a download cut off
# halfway runs nothing.
set -euo pipefail

main() {
  local repo="${AUTOBLOX_REPO:-King-Jarvis/auto-blox}"
  local ref="${AUTOBLOX_REF:-main}"
  local dir="${AUTOBLOX_DIR:-$HOME/auto-blox}"
  local tarball="${AUTOBLOX_TARBALL:-https://codeload.github.com/$repo/tar.gz/refs/heads/$ref}"
  local download_only=0
  local pass=()
  for arg in "$@"; do
    case "$arg" in
      --download-only) download_only=1 ;;
      *) pass+=("$arg") ;;
    esac
  done

  if [[ $EUID -eq 0 ]]; then
    say "run this as your own user, not as root; it asks sudo for the install step"
    exit 1
  fi
  local model=""
  model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)
  if [[ "$model" != *"Zero 2W"* && "$model" != *"Zero2W"* && "$model" != *"zero2w"* ]]; then
    say "note: this does not look like an Orange Pi Zero 2W (${model:-unknown board});"
    say "      carrying on, but the pin map and overlays are for that board"
  fi

  if [[ -d "$dir/.git" ]]; then
    say "updating the git checkout in $dir"
    if [[ -n "$(git -C "$dir" status --porcelain --untracked-files=no)" ]]; then
      say "it has local changes; commit or stash them, then run this again"
      exit 1
    fi
    git -C "$dir" pull --ff-only
  else
    fetch "$tarball" "$dir"
  fi

  if [[ $download_only == 1 ]]; then
    say "downloaded to $dir; install with: sudo $dir/scripts/install.sh"
    return
  fi
  say "installing (sudo will ask for your password)"
  # The +... form: an empty array is "unbound" to bash before 4.4.
  sudo "$dir/scripts/install.sh" ${pass[@]+"${pass[@]}"}
}

say() { printf 'auto-blox: %s\n' "$*"; }

# Download and unpack into a fresh directory, then swap it in, so a failed
# download never leaves a half-written copy where the service runs from.
fetch() {
  local src="$1" dir="$2"
  local new="$dir.new" old="$dir.previous"
  rm -rf "$new"
  mkdir -p "$new"
  say "downloading $src"
  if [[ -f "$src" ]]; then
    tar -xzf "$src" -C "$new" --strip-components=1
  elif command -v curl >/dev/null; then
    curl -fsSL "$src" | tar -xz -C "$new" --strip-components=1
  elif command -v wget >/dev/null; then
    wget -qO- "$src" | tar -xz -C "$new" --strip-components=1
  else
    say "needs curl or wget to download"
    exit 1
  fi
  if [[ ! -x "$new/scripts/install.sh" ]]; then
    say "the download does not look like Auto-Blox (no scripts/install.sh)"
    rm -rf "$new"
    exit 1
  fi
  if [[ -d "$dir" ]]; then
    # Flow snapshots are yours and are not in the download.
    if [[ -d "$dir/backups" ]]; then
      cp -a "$dir/backups/." "$new/backups/" 2>/dev/null || true
    fi
    rm -rf "$old"
    mv "$dir" "$old"
    say "the previous copy is in $old"
  fi
  mv "$new" "$dir"
}

main "$@"
