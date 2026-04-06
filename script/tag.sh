#!/usr/bin/env bash
#
# Run script within the directory
BIN_DIR=$(dirname "$(readlink -fn "$0")")
cd "${BIN_DIR}" || exit 2

set -e

cd ..

git fetch --tags origin

VERSION=$(git describe --tags)

printf "Release ${VERSION}" > "notes.txt"
gh release create ${VERSION} \
    "dist/*" \
    --title "${VERSION}" \
    -F "notes.txt" \
    --draft \
    --verify-tag \
    --fail-on-no-commits
rm notes.txt
