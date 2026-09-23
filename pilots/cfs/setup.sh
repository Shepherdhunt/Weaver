#!/bin/sh
# Prepare a pinned cFS checkout for the Weaver pilot.
#
#   pilots/cfs/setup.sh [DEST]      # default DEST: ./cfs
#   weaver -C DEST refresh --capture
#
# The checkout is cFS v7.0.1 with the submodule commits listed in submodules.txt.
# The script adds pilot_defs/ (the stock sample mission on one native CPU) and
# weaver.yaml; it changes nothing else in the cFS tree.
set -eu

DEST=${1:-cfs}
CFS_URL=https://github.com/nasa/cFS
CFS_TAG=v7.0.1
CFS_COMMIT=088b2fa828db9ff7e00733f1908e0eeb59f66ce3
HERE=$(cd "$(dirname "$0")" && pwd)

if [ ! -d "$DEST/.git" ]; then
    git clone --quiet --depth 1 --branch "$CFS_TAG" --recurse-submodules --shallow-submodules "$CFS_URL" "$DEST"
fi

head=$(git -C "$DEST" rev-parse HEAD)
if [ "$head" != "$CFS_COMMIT" ]; then
    echo "setup.sh: $DEST is at $head, expected $CFS_COMMIT ($CFS_TAG)" >&2
    exit 1
fi
git -C "$DEST" submodule status | awk '{sub(/^[-+ ]/, "", $1); print $2, $1}' | sort > "$DEST/.weaver-submodules.txt"
if ! sort "$HERE/submodules.txt" | cmp -s - "$DEST/.weaver-submodules.txt"; then
    echo "setup.sh: submodule commits differ from $HERE/submodules.txt:" >&2
    sort "$HERE/submodules.txt" | diff - "$DEST/.weaver-submodules.txt" >&2 || true
    exit 1
fi

rm -rf "$DEST/pilot_defs"
cp -R "$DEST/sample_defs" "$DEST/pilot_defs"
cp "$HERE/pilot_targets.cmake" "$DEST/pilot_defs/targets.cmake"
cp "$HERE/pilot_install_custom.cmake" "$DEST/pilot_defs/cpu1/install_custom.cmake"
cp "$HERE/pilot_generate_startup.cmake" "$DEST/pilot_defs/generate_startup.cmake"
cp "$HERE/weaver.yaml" "$DEST/weaver.yaml"
echo "cFS $CFS_TAG ready in $DEST; next: weaver -C $DEST refresh --capture"
