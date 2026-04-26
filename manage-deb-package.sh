#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME="$(basename "$0")"

DEBIAN_DIR="$SCRIPT_DIR/debian"
CHANGELOG_FILE="$DEBIAN_DIR/changelog"
CONTROL_FILE="$DEBIAN_DIR/control"

usage() {
  cat <<EOF
Usage:
  ./$SCRIPT_NAME ACTION

Actions:
  info               Show package/version information
  get-version        Print current package version
  get-package        Print binary package name
  get-source         Print source package name
  get-arch           Print package architecture

  next-major         Print next major version
  next-minor         Print next minor version
  next-patch         Print next patch version
  next-revision      Print next Debian revision

  version-major      Update debian/changelog to next major version
  version-minor      Update debian/changelog to next minor version
  version-patch      Update debian/changelog to next patch version
  version-revision   Update debian/changelog to next Debian revision

  build              Build binary package with dpkg-buildpackage -us -uc -b
  clean              Run debian/rules clean
  clean-generated    Remove generated Debian build artifacts

Version rules:
  1.2.3-1  major     -> 2.0.0-1
  1.2.3-1  minor     -> 1.3.0-1
  1.2.3-1  patch     -> 1.2.4-1
  1.2.3-1  revision  -> 1.2.3-2

Examples:
  ./$SCRIPT_NAME info
  ./$SCRIPT_NAME get-version
  ./$SCRIPT_NAME next-patch
  ./$SCRIPT_NAME version-patch
  ./$SCRIPT_NAME build
  ./$SCRIPT_NAME clean-generated
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

require_file() {
  [[ -f "$1" ]] || die "file not found: $1"
}

require_dir() {
  [[ -d "$1" ]] || die "directory not found: $1"
}

read_control_field() {
  local field="$1"

  awk -v field="$field" '
    BEGIN { IGNORECASE = 1 }

    $0 ~ "^" field ":[[:space:]]*" {
      sub("^[^:]+:[[:space:]]*", "", $0)
      print
      found = 1
      exit
    }

    END {
      if (!found) exit 1
    }
  ' "$CONTROL_FILE"
}

read_control_field_optional() {
  local field="$1"

  read_control_field "$field" 2>/dev/null || true
}

read_first_binary_package() {
  awk '
    BEGIN {
      in_binary = 0
    }

    /^Package:[[:space:]]*/ {
      sub("^Package:[[:space:]]*", "", $0)
      print
      found = 1
      exit
    }

    END {
      if (!found) exit 1
    }
  ' "$CONTROL_FILE"
}

read_first_binary_architecture() {
  awk '
    /^Package:[[:space:]]*/ {
      in_binary = 1
      next
    }

    in_binary && /^Architecture:[[:space:]]*/ {
      sub("^Architecture:[[:space:]]*", "", $0)
      print
      found = 1
      exit
    }

    END {
      if (!found) exit 1
    }
  ' "$CONTROL_FILE"
}

get_changelog_first_line() {
  head -n 1 "$CHANGELOG_FILE"
}

get_changelog_package() {
  local first_line
  local re

  first_line="$(get_changelog_first_line)"
  re='^([^[:space:]]+)[[:space:]]+\(([^)]+)\)'

  if [[ "$first_line" =~ $re ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  else
    die "cannot parse package name from debian/changelog first line"
  fi
}

get_changelog_version() {
  local first_line
  local re

  first_line="$(get_changelog_first_line)"
  re='^[^[:space:]]+[[:space:]]+\(([^)]+)\)'

  if [[ "$first_line" =~ $re ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  else
    die "cannot parse version from debian/changelog first line"
  fi
}

set_changelog_version() {
  local new_version="$1"
  local package
  package="$(get_changelog_package)"

  awk -v package="$package" -v new_version="$new_version" '
    NR == 1 {
      sub("^" package "[[:space:]]+\\([^)]*\\)", package " (" new_version ")")
      print
      next
    }

    {
      print
    }
  ' "$CHANGELOG_FILE" > "${CHANGELOG_FILE}.tmp"

  mv "${CHANGELOG_FILE}.tmp" "$CHANGELOG_FILE"
}

split_deb_version() {
  local version="$1"
  local epoch=""
  local rest="$version"
  local upstream=""
  local revision=""

  if [[ "$rest" == *:* ]]; then
    epoch="${rest%%:*}"
    rest="${rest#*:}"

    [[ "$epoch" =~ ^[0-9]+$ ]] || die "invalid epoch in version: $version"
  fi

  if [[ "$rest" == *-* ]]; then
    upstream="${rest%-*}"
    revision="${rest##*-}"
  else
    upstream="$rest"
    revision=""
  fi

  [[ -n "$upstream" ]] || die "invalid empty upstream version in: $version"

  printf '%s\n%s\n%s\n' "$epoch" "$upstream" "$revision"
}

parse_numeric_base() {
  local upstream="$1"

  if [[ "$upstream" =~ ^([0-9]+)(\.([0-9]+))?(\.([0-9]+))? ]]; then
    local major="${BASH_REMATCH[1]}"
    local minor="${BASH_REMATCH[3]:-0}"
    local patch="${BASH_REMATCH[5]:-0}"

    printf '%s\n%s\n%s\n' "$major" "$minor" "$patch"
  else
    die "cannot bump non-numeric upstream version: $upstream"
  fi
}

bump_version() {
  local version="$1"
  local bump="$2"

  local epoch upstream revision
  mapfile -t parts < <(split_deb_version "$version")
  epoch="${parts[0]}"
  upstream="${parts[1]}"
  revision="${parts[2]}"

  local major minor patch
  mapfile -t nums < <(parse_numeric_base "$upstream")
  major="${nums[0]}"
  minor="${nums[1]}"
  patch="${nums[2]}"

  case "$bump" in
    major)
      major=$((major + 1))
      minor=0
      patch=0
      revision="1"
      ;;
    minor)
      minor=$((minor + 1))
      patch=0
      revision="1"
      ;;
    patch)
      patch=$((patch + 1))
      revision="1"
      ;;
    revision)
      if [[ -z "$revision" ]]; then
        revision="1"
      elif [[ "$revision" =~ ^[0-9]+$ ]]; then
        revision=$((revision + 1))
      else
        die "cannot auto-increment non-numeric Debian revision: $revision"
      fi
      ;;
    *)
      die "invalid bump type: $bump"
      ;;
  esac

  local new_version="${major}.${minor}.${patch}"

  if [[ -n "$epoch" ]]; then
    new_version="${epoch}:${new_version}"
  fi

  if [[ -n "$revision" ]]; then
    new_version="${new_version}-${revision}"
  fi

  printf '%s\n' "$new_version"
}

show_info() {
  local changelog_package version source_package binary_package architecture maintainer description
  changelog_package="$(get_changelog_package)"
  version="$(get_changelog_version)"
  source_package="$(read_control_field "Source")"
  binary_package="$(read_first_binary_package)"
  architecture="$(read_first_binary_architecture)"
  maintainer="$(read_control_field_optional "Maintainer")"
  description="$(read_control_field_optional "Description")"

  local epoch upstream revision
  mapfile -t parts < <(split_deb_version "$version")
  epoch="${parts[0]}"
  upstream="${parts[1]}"
  revision="${parts[2]}"

  local major minor patch
  mapfile -t nums < <(parse_numeric_base "$upstream")
  major="${nums[0]}"
  minor="${nums[1]}"
  patch="${nums[2]}"

  printf 'Changelog package: %s\n' "$changelog_package"
  printf 'Source package:    %s\n' "$source_package"
  printf 'Binary package:    %s\n' "$binary_package"
  printf 'Version:           %s\n' "$version"
  printf 'Architecture:      %s\n' "$architecture"

  if [[ -n "$maintainer" ]]; then
    printf 'Maintainer:        %s\n' "$maintainer"
  fi

  if [[ -n "$description" ]]; then
    printf 'Description:       %s\n' "$description"
  fi

  printf '\n'
  printf 'Epoch:             %s\n' "${epoch:-none}"
  printf 'Upstream:          %s\n' "$upstream"
  printf 'Major:             %s\n' "$major"
  printf 'Minor:             %s\n' "$minor"
  printf 'Patch:             %s\n' "$patch"
  printf 'Revision:          %s\n' "${revision:-none}"

  printf '\n'
  printf 'Next major:        %s\n' "$(bump_version "$version" "major")"
  printf 'Next minor:        %s\n' "$(bump_version "$version" "minor")"
  printf 'Next patch:        %s\n' "$(bump_version "$version" "patch")"
  printf 'Next revision:     %s\n' "$(bump_version "$version" "revision")"
}

update_version() {
  local bump="$1"
  local old_version new_version

  old_version="$(get_changelog_version)"
  new_version="$(bump_version "$old_version" "$bump")"

  set_changelog_version "$new_version"

  printf 'Old version: %s\n' "$old_version"
  printf 'New version: %s\n' "$new_version"
}

build_package() {
  need_cmd dpkg-buildpackage

  (
    cd "$SCRIPT_DIR"
    dpkg-buildpackage -us -uc -b
  )
}

clean_package() {
  require_file "$DEBIAN_DIR/rules"

  (
    cd "$SCRIPT_DIR"
    debian/rules clean
  )
}

clean_generated() {
  local source_package binary_package

  source_package="$(read_control_field "Source")"
  binary_package="$(read_first_binary_package)"

  rm -rf \
    "$DEBIAN_DIR/.debhelper" \
    "$DEBIAN_DIR/debhelper-build-stamp" \
    "$DEBIAN_DIR/files" \
    "$DEBIAN_DIR/tmp" \
    "$DEBIAN_DIR/$binary_package"

  rm -f \
    "$DEBIAN_DIR"/*.substvars \
    "$DEBIAN_DIR"/*.debhelper

  find "$SCRIPT_DIR/.." -maxdepth 1 -type f \
    \( -name "${source_package}_*.build" \
       -o -name "${source_package}_*.buildinfo" \
       -o -name "${source_package}_*.changes" \
       -o -name "${source_package}_*.deb" \
       -o -name "${source_package}_*.dsc" \
       -o -name "${source_package}_*.tar.*" \
       -o -name "${binary_package}_*.build" \
       -o -name "${binary_package}_*.buildinfo" \
       -o -name "${binary_package}_*.changes" \
       -o -name "${binary_package}_*.deb" \
       -o -name "${binary_package}_*.dsc" \
       -o -name "${binary_package}_*.tar.*" \) \
    -print -delete
}

ACTION="${1:-}"

if [[ -z "$ACTION" || "$ACTION" == "-h" || "$ACTION" == "--help" ]]; then
  usage
  exit 0
fi

need_cmd awk

require_dir "$DEBIAN_DIR"
require_file "$CHANGELOG_FILE"
require_file "$CONTROL_FILE"

CURRENT_VERSION="$(get_changelog_version)"

case "$ACTION" in
  info)
    show_info
    ;;
  get-version)
    printf '%s\n' "$CURRENT_VERSION"
    ;;
  get-package)
    read_first_binary_package
    ;;
  get-source)
    read_control_field "Source"
    ;;
  get-arch)
    read_first_binary_architecture
    ;;
  next-major)
    bump_version "$CURRENT_VERSION" "major"
    ;;
  next-minor)
    bump_version "$CURRENT_VERSION" "minor"
    ;;
  next-patch)
    bump_version "$CURRENT_VERSION" "patch"
    ;;
  next-revision)
    bump_version "$CURRENT_VERSION" "revision"
    ;;
  version-major)
    update_version "major"
    ;;
  version-minor)
    update_version "minor"
    ;;
  version-patch)
    update_version "patch"
    ;;
  version-revision)
    update_version "revision"
    ;;
  build)
    build_package
    ;;
  clean)
    clean_package
    ;;
  clean-generated)
    clean_generated
    ;;
  *)
    die "invalid action: $ACTION"
    ;;
esac
