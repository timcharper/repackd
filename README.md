# repackd

On-demand `.deb` -> `.rpm` repo shim. Tracks a vendor's **signed apt repo** and
serves it to `dnf` as a localhost repo, so `dnf upgrade` keeps proprietary
Debian-only packages current with no third-party rebuilder in the trust chain.
First recipe: Claude Desktop for Linux.

## How it works

- `repackd.socket` listens on `127.0.0.1:8742`; `repackd.service` (DynamicUser,
  no root) starts on the first connection and exits after 60s idle.
- A request for `/<recipe>/repodata/repomd.xml` triggers a sync (at most once per
  30 min, `REPACKD_CHECK_MINUTES`):
  1. `InRelease` is verified with `gpgv` against the key pinned in `/etc/repackd/keys`.
  2. `Packages` and the `.deb` are verified against the signed SHA256/size chain.
  3. If the version changed, the deb is unpacked, a spec is generated, `rpmbuild`
     runs, the RPM is signed with a local key, and `createrepo_c` updates metadata.
- Upstream down or sync failed: the existing repo is served and the error is in the journal.
- Everything else is a static file from `/var/lib/repackd/<recipe>/repo`.

Version mapping: `[N:]upstream[-rev]` -> `Epoch: N`, `Version: upstream`,
`Release: <rev|1>.repackd`.

Maintainer scripts (`pre/postinst`, `pre/postrm`) are not converted. Their hashes
are tracked; if upstream changes them you get a journal warning and a diff in
`/var/lib/repackd/<recipe>/diffs/`. `control_change = "block"` (recipe) or
`REPACKD_CONTROL_CHANGE=block` refuses the update until you review it and run
`repackd approve <recipe>`.

## Bounded storage

Nothing grows without limit: only the newest `keep` RPMs (default 3) are retained
(older ones are deleted on each build, which is also your rollback window); the
download/extract work dir is removed after every sync (and on startup after a
crash); control-script diffs are capped at 5; old repodata is not retained.
The `.deb` is never kept.

## Install / try

    sudo ./install.sh          # pins the upstream key (shows fingerprint, asks first)
    sudo dnf makecache --refresh --repo repackd-claude-desktop
    sudo dnf install claude-desktop

Compare the printed fingerprint with Anthropic's docs. Fill in `key_fingerprint`
in the recipe to make this a hard check. Weak deps: Cowork wants KVM/QEMU
(`qemu-kvm` is a Recommends) plus the one-time `kvm` group step from the docs.

## Adding a package

Copy `recipes/claude-desktop.toml`, change the apt URL/package/key, strip list,
setuid files, bundled dir and extra deps, then re-run `install.sh`. Do not
publish the built RPMs anywhere; most such software isn't redistributable.

## Tests

    python3 -m unittest discover -s tests -v

Builds a fake signed apt repo (ELF binary, bundled `.so`, setuid helper, postinst)
and covers first build, no-op, bump, bad signature, sha mismatch, upstream down,
control-script warn/block/approve, soname exclusion, setuid, signing, pruning.

## Uninstall

    sudo systemctl disable --now repackd.socket repackd.service
    sudo rm -f /etc/systemd/system/repackd.{socket,service} /etc/yum.repos.d/repackd-*.repo /usr/local/bin/repackd
    sudo rm -rf /etc/repackd /var/lib/repackd /var/lib/private/repackd
    sudo dnf remove claude-desktop    # optional; the RPM stays installed otherwise
