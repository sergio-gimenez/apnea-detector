#!/bin/sh
set -eu

# Redeploy the running LXC from git.
#
# install-lxc.sh builds a release from whatever source tree it is invoked from,
# so a deploy used to mean copying the whole repo onto the box again. Keeping one
# checkout and pulling into it means a redeploy transfers only the new commits,
# and the box stops accumulating throwaway copies of the source.
#
#   ssh apnea-detector 'sh /opt/apnea-detector/src/deploy/update.sh'
#
# Bootstrap on a box that has no checkout yet:
#   ssh apnea-detector 'git clone -b main <repo> /opt/apnea-detector/src \
#       && sh /opt/apnea-detector/src/deploy/update.sh'

umask 022

CHECKOUT=${APNEA_CHECKOUT:-/opt/apnea-detector/src}
BRANCH=${APNEA_BRANCH:-main}
KEEP=${APNEA_KEEP_RELEASES:-3}
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' "Run as root inside the dedicated LXC." >&2
    exit 1
fi

if [ ! -d "$CHECKOUT/.git" ]; then
    printf '%s\n' "No git checkout at $CHECKOUT. Clone the repo there first." >&2
    exit 1
fi

cd "$CHECKOUT"
BEFORE=$(git rev-parse --short HEAD)
git fetch --quiet origin "$BRANCH"
# this checkout exists only to build releases from, so local edits are not worth
# preserving — but say so, rather than discarding them silently
if ! git diff --quiet || ! git diff --cached --quiet; then
    printf '%s\n' "Discarding local changes in $CHECKOUT" >&2
fi
git reset --hard --quiet "origin/$BRANCH"
AFTER=$(git rev-parse --short HEAD)

if [ "$BEFORE" = "$AFTER" ] && [ "$FORCE" -eq 0 ]; then
    printf '%s\n' "Already at $AFTER on $BRANCH. Nothing to deploy (--force to rebuild anyway)."
    exit 0
fi
printf '%s\n' "Deploying $BEFORE -> $AFTER ($BRANCH)"
git --no-pager log --oneline "$BEFORE..$AFTER" 2>/dev/null || true

sh "$CHECKOUT/deploy/install-lxc.sh"

# keep a few releases to roll back to, drop the rest
CURRENT=$(basename "$(readlink /opt/apnea-detector/current)")
ls -1 /opt/apnea-detector/releases | sort -r | tail -n "+$((KEEP + 1))" | while read -r old; do
    [ "$old" = "$CURRENT" ] && continue
    rm -rf "/opt/apnea-detector/releases/$old"
    printf '%s\n' "Pruned release $old"
done

printf '%s\n' "Now serving $AFTER from release $CURRENT"
