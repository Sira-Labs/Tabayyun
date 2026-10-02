#!/usr/bin/env bash
# The release tag for the commit SHA (default HEAD), from the existing v* tags (cut-release.yml,
# ADR-0017).
#
#   BUMP=patch|minor|major PRERELEASE=true|false [SHA=<commit>] next-version.sh
#
# Only tags in SHA's history count, so a tag on an unmerged branch never sets the base. A
# re-run after a failed publish reuses the tag of the same kind (stable or candidate) that
# already points at SHA, so the version is not skipped; it fails when that tag is not what
# BUMP would give. Otherwise the next tag is computed; without any tag the base is v0.0.0,
# so the first minor release is v0.1.0.
#
# Prints, and appends to $GITHUB_OUTPUT when set:
#   tag=     vX.Y.Z, or vX.Y.Z-rc.N for a release candidate
#   prev=    the release the notes start from: the stable tag before it for a stable release,
#            the candidate of the same version before it (else the stable tag before it) for
#            a candidate; empty for the first release
#   reused=  true when tag already existed at SHA
#   review=  true for a stable minor or major release (it gets a presentation and a meeting)
set -euo pipefail

bump="${BUMP:?BUMP must be patch, minor or major}"
pre="${PRERELEASE:-false}"
sha=$(git rev-parse "${SHA:-HEAD}^{commit}")
stable_re='^v[0-9]+\.[0-9]+\.[0-9]+$'
case "$bump" in patch | minor | major) ;; *) echo "::error::BUMP must be patch, minor or major, not '$bump'" >&2; exit 2 ;; esac

# Tags in SHA's history sorted by version, newest first; `below TAG` lists those older than
# TAG; `bumped BASE` is the version BUMP gives after the stable tag BASE (empty: v0.0.0).
tags() { git tag --list "$1" --merged "$sha" --sort=-v:refname; }
stable_tags() { tags 'v*' | grep -E "$stable_re" || true; }
below() { awk -v t="$1" 'found { print } $0 == t { found = 1 }'; }
bumped() {
  local major minor patch
  IFS=. read -r major minor patch <<< "${1#v}"
  major=${major:-0} minor=${minor:-0} patch=${patch:-0}
  case "$bump" in
    major) echo "v$((major + 1)).0.0" ;;
    minor) echo "v$major.$((minor + 1)).0" ;;
    patch) echo "v$major.$minor.$((patch + 1))" ;;
  esac
}

if [ "$pre" = "true" ]; then kind_re='^v[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9]+$'; else kind_re="$stable_re"; fi
existing=$(git tag --points-at "$sha" --list 'v*' --sort=-v:refname | grep -E "$kind_re" | head -n 1 || true)

if [ -n "$existing" ]; then
  tag="$existing"
  reused=true
  version="${tag%%-rc.*}"
  # The stable release before this version (the version itself may exist as a stable tag).
  base=$({ stable_tags; echo "$version"; } | sort -u -V -r | below "$version" | head -n 1)
  if [ "$(bumped "$base")" != "$version" ]; then
    echo "::error::$tag already points at this commit but is not a $bump release after ${base:-v0.0.0}; re-run with the bump that made it" >&2
    exit 1
  fi
  if [ "$pre" = "true" ]; then
    prev=$(tags "$version-rc.*" | below "$tag" | head -n 1)
    if [ -z "$prev" ]; then prev="$base"; fi
  else
    prev="$base"
  fi
else
  reused=false
  stable=$(stable_tags | head -n 1)
  version=$(bumped "$stable")
  prev="$stable"
  if [ "$pre" = "true" ]; then
    last_rc=$(tags "$version-rc.*" | head -n 1)
    n=1
    if [ -n "$last_rc" ]; then n=$(( ${last_rc##*-rc.} + 1 )); prev="$last_rc"; fi
    tag="$version-rc.$n"
  else
    tag="$version"
  fi
  if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    echo "::error::$tag already exists on another commit" >&2
    exit 1
  fi
fi

review=false
if [[ "$tag" =~ $stable_re ]] && [[ "$tag" == *.0 ]]; then review=true; fi
for line in "tag=$tag" "prev=$prev" "reused=$reused" "review=$review"; do
  echo "$line"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then echo "$line" >> "$GITHUB_OUTPUT"; fi
done
