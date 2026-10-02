#!/usr/bin/env bash
# The next release tag from the existing v* tags (cut-release.yml, ADR-0017).
#
#   BUMP=patch|minor|major PRERELEASE=true|false next-version.sh
#
# Prints, and appends to $GITHUB_OUTPUT when set:
#   tag=     vX.Y.Z, or vX.Y.Z-rc.N for a release candidate
#   prev=    the release the notes start from: the last stable tag for a stable release, the
#            last candidate of the same version (else the last stable tag) for a candidate;
#            empty for the first release
#   review=  true for a stable minor or major release (it gets a presentation and a meeting)
# Without any tag the base is v0.0.0, so the first minor release is v0.1.0.
set -euo pipefail

bump="${BUMP:?BUMP must be patch, minor or major}"
pre="${PRERELEASE:-false}"
stable=$(git tag --list 'v*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n 1 || true)
IFS=. read -r major minor patch <<< "${stable#v}"
major=${major:-0} minor=${minor:-0} patch=${patch:-0}
case "$bump" in
  major) major=$((major + 1)) minor=0 patch=0 ;;
  minor) minor=$((minor + 1)) patch=0 ;;
  patch) patch=$((patch + 1)) ;;
  *) echo "::error::BUMP must be patch, minor or major, not '$bump'" >&2; exit 2 ;;
esac
version="v$major.$minor.$patch"
prev="$stable"
review=false
if [ "$pre" = "true" ]; then
  last_rc=$(git tag --list "$version-rc.*" --sort=-v:refname | head -n 1 || true)
  n=1
  if [ -n "$last_rc" ]; then n=$(( ${last_rc##*-rc.} + 1 )); prev="$last_rc"; fi
  tag="$version-rc.$n"
else
  tag="$version"
  if [ "$bump" != "patch" ]; then review=true; fi
fi
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "::error::$tag already exists" >&2
  exit 1
fi
for line in "tag=$tag" "prev=$prev" "review=$review"; do
  echo "$line"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then echo "$line" >> "$GITHUB_OUTPUT"; fi
done
