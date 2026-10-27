#!/bin/bash
# Install repackd: engine, units, recipes, pinned upstream keys, dnf .repo files.
# Usage: sudo ./install.sh [--recipes a,b | --all] [--yes] [--port 8742]
#        ./install.sh --list
# One dnf repo ("repackd") serves every enabled recipe. A recipe is enabled when its
# .toml is in /etc/repackd/recipes; re-run with a different --recipes to change the set.
# With neither flag, the recipes already installed are kept.
set -euo pipefail

PORT=8742
YES=0
ALL=0
WANT_RECIPES=
LIST=0
while [ $# -gt 0 ]; do
  case $1 in
    --yes) YES=1 ;;
    --port) PORT=$2; shift ;;
    --recipes) WANT_RECIPES=$2; shift ;;
    --all) ALL=1 ;;
    --list) LIST=1 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
  shift
done
HERE=$(cd "$(dirname "$0")" && pwd)
AVAILABLE=$(cd "$HERE/recipes" && for f in *.toml; do echo "${f%.toml}"; done)
if [ $LIST = 1 ]; then echo "$AVAILABLE"; exit 0; fi
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

if [ $ALL = 1 ]; then
  SELECTED=$AVAILABLE
elif [ -n "$WANT_RECIPES" ]; then
  SELECTED=${WANT_RECIPES//,/ }
else
  SELECTED=$(cd /etc/repackd/recipes 2>/dev/null && for f in *.toml; do [ -e "$f" ] && echo "${f%.toml}"; done || true)
  if [ -z "$SELECTED" ]; then
    echo "choose recipes: --recipes a,b or --all. Available:" >&2
    echo "$AVAILABLE" | sed 's/^/    /' >&2
    exit 2
  fi
fi
for r in $SELECTED; do
  echo "$AVAILABLE" | grep -qx -- "$r" || { echo "unknown recipe: $r (see --list)" >&2; exit 2; }
done

for t in rpmbuild rpmsign createrepo_c gpg gpgv ar tar python3 curl; do
  command -v "$t" >/dev/null || { echo "missing $t; try: dnf install rpm-build rpm-sign createrepo_c gnupg2 binutils tar python3 curl zstd xz" >&2; exit 1; }
done

install -Dm755 "$HERE/repackd" /usr/local/bin/repackd
install -dm755 /etc/repackd/recipes /etc/repackd/keys
# Enable exactly the selected recipes (and the scriptlet files they name).
find /etc/repackd/recipes -maxdepth 1 -type f \( -name '*.toml' -o -name '*.spec' \) -delete
for r in $SELECTED; do
  install -m644 "$HERE/recipes/$r.toml" /etc/repackd/recipes/
  sl=$(python3 -I -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb")).get("build", {}).get("scriptlets", ""))' "$HERE/recipes/$r.toml")
  [ -z "$sl" ] || install -m644 "$HERE/recipes/$sl" /etc/repackd/recipes/
done
install -m644 "$HERE"/systemd/repackd.socket /etc/systemd/system/repackd.socket
install -m644 "$HERE"/systemd/repackd.service /etc/systemd/system/repackd.service
sed -i "s/^ListenStream=.*/ListenStream=127.0.0.1:$PORT/" /etc/systemd/system/repackd.socket
restorecon -R /usr/local/bin/repackd /etc/repackd 2>/dev/null || true

# Pin each apt recipe's upstream signing key (trust on first use unless the recipe has key_fingerprint).
# [direct] recipes have no signing key; they are trusted over TLS only.
for recipe in /etc/repackd/recipes/*.toml; do
  eval "$(python3 -I - "$recipe" <<'PY'
import sys, tomllib, shlex
d = tomllib.load(open(sys.argv[1], "rb"))
a = d.get("apt", {})
print("NAME=%s KEYURL=%s KEYRING=%s WANT=%s SUMMARY=%s" % tuple(shlex.quote(x) for x in (
    d["name"], a.get("key_url", ""), a.get("keyring", ""), a.get("key_fingerprint", ""), d["summary"])))
PY
)"
  dest=/etc/repackd/keys/$KEYRING
  if [ -z "$KEYRING" ]; then
    echo "[$NAME] direct download, no signing key; trust is TLS only"
  elif [ -f "$dest" ]; then
    echo "[$NAME] key already pinned: $dest"
  else
    tmp=$(mktemp -d)
    curl -fsSL "$KEYURL" -o "$tmp/key.asc"
    fprs=$(gpg --show-keys --with-colons "$tmp/key.asc" | awk -F: '$1=="fpr"{print $10}')
    echo "[$NAME] upstream key fingerprints from $KEYURL:"
    echo "$fprs" | sed 's/^/    /'
    if [ -n "$WANT" ]; then
      echo "$fprs" | grep -qix "${WANT// /}" || { echo "[$NAME] fingerprint does not match recipe key_fingerprint; refusing" >&2; exit 1; }
    elif [ $YES = 0 ]; then
      echo "    Compare with Anthropic's docs (https://code.claude.com/docs/en/desktop-linux) before trusting."
      read -r -p "Pin this key? [y/N] " a
      [ "$a" = y ] || { echo aborted >&2; exit 1; }
    fi
    gpg --dearmor <"$tmp/key.asc" >"$dest"
    chmod 644 "$dest"
    rm -rf "$tmp"
  fi
  echo "[$NAME] enabled"
done

# One repo for all recipes; drop the per-recipe repo files of older versions.
rm -f /etc/yum.repos.d/repackd-*.repo
sed -e "s|@PORT@|$PORT|g" "$HERE/systemd/repackd.repo.in" >/etc/yum.repos.d/repackd.repo
echo "wrote /etc/yum.repos.d/repackd.repo"

systemctl daemon-reload
systemctl enable --now repackd.socket
# first connection starts the service, which generates the local signing key
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null
curl -fsS "http://127.0.0.1:$PORT/RPM-GPG-KEY-repackd" | gpg --show-keys --with-colons | awk -F: '$1=="fpr"{print "local signing key fingerprint: " $10; exit}'
echo "done. Try: dnf makecache --refresh --repo repackd   (enabled: $(echo $SELECTED))"
