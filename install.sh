#!/bin/bash
# Install repackd: engine, units, recipes, pinned upstream keys, dnf .repo files.
# Usage: sudo ./install.sh [--yes] [--port 8742]
set -euo pipefail

PORT=8742
YES=0
while [ $# -gt 0 ]; do
  case $1 in
    --yes) YES=1 ;;
    --port) PORT=$2; shift ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
  shift
done
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)

for t in rpmbuild rpmsign createrepo_c gpg gpgv ar tar python3 curl; do
  command -v "$t" >/dev/null || { echo "missing $t; try: dnf install rpm-build rpm-sign createrepo_c gnupg2 binutils tar python3 curl zstd xz" >&2; exit 1; }
done

install -Dm755 "$HERE/repackd" /usr/local/bin/repackd
install -dm755 /etc/repackd/recipes /etc/repackd/keys
install -m644 "$HERE"/recipes/*.toml /etc/repackd/recipes/
install -m644 "$HERE"/systemd/repackd.socket /etc/systemd/system/repackd.socket
install -m644 "$HERE"/systemd/repackd.service /etc/systemd/system/repackd.service
sed -i "s/^ListenStream=.*/ListenStream=127.0.0.1:$PORT/" /etc/systemd/system/repackd.socket
restorecon -R /usr/local/bin/repackd /etc/repackd 2>/dev/null || true

# Pin each recipe's upstream signing key (trust on first use unless the recipe has key_fingerprint).
for recipe in /etc/repackd/recipes/*.toml; do
  eval "$(python3 -I - "$recipe" <<'PY'
import sys, tomllib, shlex
d = tomllib.load(open(sys.argv[1], "rb"))
print("NAME=%s KEYURL=%s KEYRING=%s WANT=%s SUMMARY=%s" % tuple(shlex.quote(x) for x in (
    d["name"], d["apt"]["key_url"], d["apt"]["keyring"], d["apt"].get("key_fingerprint", ""), d["summary"])))
PY
)"
  dest=/etc/repackd/keys/$KEYRING
  if [ -f "$dest" ]; then
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
  sed -e "s|@NAME@|$NAME|g" -e "s|@PORT@|$PORT|g" -e "s|@SUMMARY@|$SUMMARY|g" \
      "$HERE/systemd/repackd.repo.in" >"/etc/yum.repos.d/repackd-$NAME.repo"
  echo "[$NAME] wrote /etc/yum.repos.d/repackd-$NAME.repo"
done

systemctl daemon-reload
systemctl enable --now repackd.socket
# first connection starts the service, which generates the local signing key
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null
curl -fsS "http://127.0.0.1:$PORT/RPM-GPG-KEY-repackd" | gpg --show-keys --with-colons | awk -F: '$1=="fpr"{print "local signing key fingerprint: " $10; exit}'
echo "done. Try: dnf makecache --refresh --repo repackd-claude-desktop"
