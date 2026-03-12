#!/usr/bin/env bash
# scripts/release.sh — cut a Penny release and update the Homebrew formula
#
# Usage:
#   bash scripts/release.sh [--dry-run] [VERSION]
#
# Examples:
#   bash scripts/release.sh 0.2.0          # full release
#   bash scripts/release.sh --dry-run 0.2.0  # print actions, push nothing

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FORMULA="$PROJECT_ROOT/Formula/penny.rb"
PYPROJECT="$PROJECT_ROOT/pyproject.toml"
GITHUB_REPO="gpxl/penny"

DRY_RUN=false
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    *) ARGS+=("$arg") ;;
  esac
done

# ── Get version ───────────────────────────────────────────────────────────────
if [[ ${#ARGS[@]} -gt 0 ]]; then
  VERSION="${ARGS[0]}"
else
  read -rp "Version (e.g. 0.2.0): " VERSION
fi
VERSION="${VERSION#v}"   # strip leading 'v' if provided
TAG="v$VERSION"
ARCHIVE_URL="https://github.com/$GITHUB_REPO/archive/refs/tags/$TAG.tar.gz"

echo ""
echo "==> Release: $TAG"
[[ "$DRY_RUN" == true ]] && echo "    (dry-run — no changes will be pushed)"
echo ""

cd "$PROJECT_ROOT"

# ── 1. Validate clean working tree ───────────────────────────────────────────
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "error: working tree is not clean. Commit or stash changes first." >&2
  exit 1
fi

# ── 2. Bump version in pyproject.toml and penny/__init__.py ─────────────────
CURRENT=$(grep '^version = ' "$PYPROJECT" | head -1 | sed 's/version = "\(.*\)"/\1/')
echo "    pyproject.toml: $CURRENT -> $VERSION"
echo "    penny/__init__.py: -> $VERSION"

if [[ "$DRY_RUN" == false ]]; then
  sed -i '' "s/^version = \"$CURRENT\"/version = \"$VERSION\"/" "$PYPROJECT"
  sed -i '' "s/__version__ = \".*\"/__version__ = \"$VERSION\"/" penny/__init__.py
fi

# ── 3. Commit, tag, push ─────────────────────────────────────────────────────
if [[ "$DRY_RUN" == false ]]; then
  git add "$PYPROJECT" penny/__init__.py
  git commit -m "chore: bump version to $TAG"
  git tag "$TAG"
  git push
  git push --tags
  echo "    Pushed $TAG to GitHub."
else
  echo "    [dry-run] would bump pyproject.toml + penny/__init__.py, commit, tag, and push"
fi

# ── 4. Compute sha256 of release archive ─────────────────────────────────────
echo ""
echo "==> Computing sha256 for archive"
echo "    $ARCHIVE_URL"

if [[ "$DRY_RUN" == false ]]; then
  echo "    Waiting 5s for GitHub to process the tag..."
  sleep 5
  SHA256=$(curl -fsSL "$ARCHIVE_URL" | shasum -a 256 | awk '{print $1}')
  echo "    sha256: $SHA256"
else
  SHA256="<sha256-computed-at-release-time>"
  echo "    [dry-run] would download archive and compute sha256"
fi

# ── 5. Update Formula/penny.rb ──────────────────────────────────────────────
echo ""
echo "==> Updating Formula/penny.rb"

if [[ "$DRY_RUN" == false ]]; then
  # Replace url and the sha256 that immediately follows it (main formula sha256,
  # not the resource sha256s) in a single awk pass.
  awk -v new_url="$ARCHIVE_URL" -v sha="$SHA256" '
    /url "https:\/\/github.com\/gpxl\/penny\/archive/ {
      print "  url \"" new_url "\""
      next_is_sha = 1
      next
    }
    next_is_sha {
      print "  sha256 \"" sha "\""
      next_is_sha = 0
      next
    }
    { print }
  ' "$FORMULA" > "$FORMULA.tmp" && mv "$FORMULA.tmp" "$FORMULA"
  echo "    Updated url and sha256."
else
  echo "    [dry-run] would set:"
  echo "      url    $ARCHIVE_URL"
  echo "      sha256 $SHA256"
fi

# ── 6. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "✅  $TAG tagged and pushed."
echo ""
echo "Next: update the tap repo"
echo "  cd ~/homebrew-penny   # or wherever you cloned gpxl/homebrew-penny"
echo "  cp \"$FORMULA\" Formula/penny.rb"
echo "  git commit -am \"penny $VERSION\""
echo "  git push"
echo ""
echo "Then users can install with:"
echo "  brew tap gpxl/penny && brew install penny"
