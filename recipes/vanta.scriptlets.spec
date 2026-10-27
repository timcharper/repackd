# Replaces the vendor postinst/prerm/postrm, which sniff /sbin/init for sysv/upstart.
# Percent signs are doubled: this text goes through rpm macro expansion.

%post
# Optional on install, from the environment or /etc/repackd/vanta.env: VANTA_KEY,
# VANTA_OWNER_EMAIL, VANTA_REGION, VANTA_NOSTART.
# Without a key the host stays unregistered; an upgrade never touches /etc/vanta.conf.
conf=/etc/vanta.conf
# On a fresh install with no key in the environment, read an optional root-only file of plain
# KEY=value lines (never sourced), kept outside any repo: /etc/repackd/vanta.env
envf=/etc/repackd/vanta.env
if [ -z "$VANTA_KEY" ] && [ "$1" -eq 1 ] && [ -f "$envf" ]; then
  case "$(stat -c '%%u %%a' "$envf")" in
    "0 600"|"0 400")
      while IFS='=' read -r k v; do
        v=${v#\"}; v=${v%%\"}; v=${v#\'}; v=${v%%\'}
        case "$k" in
          VANTA_KEY|VANTA_OWNER_EMAIL|VANTA_REGION|VANTA_NOSTART) export "$k=$v" ;;
        esac
      done <"$envf" ;;
    *) echo "vanta: ignoring $envf (must be owned by root, mode 0600 or 0400)" >&2 ;;
  esac
fi
if [ -z "$VANTA_KEY" ]; then
  if [ "$1" -eq 1 ]; then
    echo "vanta: installed without registration. To register:"
    echo "  /var/vanta/vanta-cli register --secret=<key> --email=<email>"
  fi
elif [ -z "$VANTA_OWNER_EMAIL" ]; then
  echo "vanta: VANTA_KEY was set but VANTA_OWNER_EMAIL was not; registration needs both." >&2
  exit 1
else
  case "$VANTA_KEY$VANTA_OWNER_EMAIL$VANTA_REGION" in
    *\"*|*\\*) echo "vanta: quotes and backslashes are not allowed in registration values." >&2; exit 1 ;;
  esac
  (umask 077; printf '{"ACTIVATION_REQUESTED_NONCE":%%s000,"AGENT_KEY":"%%s","OWNER_EMAIL":"%%s","REGION":"%%s","NEEDS_OWNER":true}\n' \
    "$(date +%%s)" "$VANTA_KEY" "$VANTA_OWNER_EMAIL" "$VANTA_REGION" >"$conf")
fi
chmod 600 "$conf"
chown root:root "$conf"

if command -v semanage >/dev/null 2>&1; then
  semanage fcontext -a -t bin_t "/var/vanta/.*" >/dev/null 2>&1 || true
fi
command -v restorecon >/dev/null 2>&1 && restorecon -R /var/vanta || true

systemctl daemon-reload >/dev/null 2>&1 || true
systemctl enable vanta.service >/dev/null 2>&1 || true
if [ -n "$VANTA_NOSTART" ]; then
  echo "vanta: not started. To start: systemctl start vanta.service"
else
  systemctl restart vanta.service >/dev/null 2>&1 || true
fi

%preun
if [ "$1" -eq 0 ]; then
  systemctl stop vanta.service >/dev/null 2>&1 || true
  systemctl disable vanta.service >/dev/null 2>&1 || true
fi

%postun
if [ "$1" -eq 0 ]; then
  rm -rf /var/vanta
  rm -f /usr/local/bin/vanta-cli
  if command -v semanage >/dev/null 2>&1; then
    semanage fcontext -d "/var/vanta/.*" >/dev/null 2>&1 || true
  fi
  systemctl daemon-reload >/dev/null 2>&1 || true
fi
