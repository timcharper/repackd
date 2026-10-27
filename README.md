# repackd

On-demand `.deb` -> `.rpm` repo shim. Tracks a vendor's **signed apt repo** and
serves it to `dnf` as a localhost repo, so `dnf upgrade` keeps proprietary
Debian-only packages current with no third-party rebuilder in the trust chain.
Recipes: Claude Desktop for Linux (signed apt repo) and the Vanta agent (direct download, TLS only).

## How it works

- `repackd.socket` listens on `127.0.0.1:8742`; `repackd.service` (DynamicUser,
  no root) starts on the first connection and exits after 60s idle.
- dnf sees one repo, `repackd` (`/repo/`), holding every enabled recipe's RPMs with one set of
  metadata. A recipe is enabled when its `.toml` is in `/etc/repackd/recipes`.
- A request for `/repo/repodata/repomd.xml` syncs every enabled recipe in parallel, each at most once
  per 30 min (`REPACKD_CHECK_MINUTES`); one recipe failing leaves its old RPMs served and does not
  block the others. Per recipe:
  1. `InRelease` is verified with `gpgv` against the key pinned in `/etc/repackd/keys`.
  2. `Packages` and the `.deb` are verified against the signed SHA256/size chain.
  3. If the version changed, the deb is unpacked, a spec is generated, `rpmbuild`
     runs, the RPM is signed with a local key, and `createrepo_c` updates metadata.
- A `[direct]` recipe (no apt repo, no signatures) is different: a `HEAD` on its `url`
(not followed) yields a redirect to the `.deb`. A redirect target already built, or already blocked,
is "no update" and nothing is downloaded. A new target is downloaded (TLS is the only trust,
the sha256 is recorded in `state.json`), and the version comes from the deb's own `control`.
A failed build does not record the target, so it is retried. The redirect must be a single hop.

Upstream down or sync failed: the existing repo is served and the error is in the journal.
- Each recipe builds into `/var/lib/repackd/<recipe>/repo`; `/var/lib/repackd/repo` hardlinks them together.

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

    ./install.sh --list                                  # available recipes
    sudo ./install.sh --recipes claude-desktop,vanta     # or --all; pins apt keys (shows fingerprint, asks first)
    sudo dnf makecache --refresh --repo repackd
    sudo dnf install claude-desktop vanta

Re-run `install.sh` with a different `--recipes` to change what is enabled (a deselected recipe stops
being served; its installed package stays). Without flags it keeps the current set. Installed packages
from older per-recipe repos (`repackd-<recipe>`) keep working; `install.sh` replaces those repo files with
the single `repackd` one.

Compare the printed fingerprint with Anthropic's docs. Fill in `key_fingerprint`
in the recipe to make this a hard check. Weak deps: Cowork wants KVM/QEMU
(`qemu-kvm` is a Recommends) plus the one-time `kvm` group step from the docs.

## Vanta agent

    sudo dnf install vanta                                     # installs unregistered
    sudo /var/vanta/vanta-cli register --secret=<key> --email=<email>

To register at install time, put the values in a root-only file outside any repo before the first install:

    sudo install -m600 /dev/null /etc/repackd/vanta.env
    sudoedit /etc/repackd/vanta.env       # VANTA_KEY=...  VANTA_OWNER_EMAIL=...  VANTA_REGION=us  (plain KEY=value lines)

It is read only on a fresh install (never on upgrade), parsed rather than sourced, and ignored unless it
is owned by root with mode 0600 or 0400. Variables in the environment win
(`sudo env VANTA_KEY=... VANTA_OWNER_EMAIL=... dnf install vanta`, which lands in shell history).
`VANTA_NOSTART=1` skips the start. The file may be deleted once the host is registered. No secret is ever
in a recipe, the RPM or this repo; `/etc/repackd/vanta.env` is yours and `install.sh` never touches it. The recipe's `scriptlets` file replaces the vendor maintainer scripts (those sniff
sysvinit/upstart); the vendor scripts stay hash-tracked, so a change still gets a warning and a diff.
`/etc/vanta.conf` is `%config(noreplace)`: an upgrade never overwrites a registered host's config
(a new default may appear as `/etc/vanta.conf.rpmnew`). x86_64 only; the endpoint has no arm64 path.

## Adding a package

Copy `recipes/claude-desktop.toml` (apt) or `recipes/vanta.toml` (direct), change the source,
strip list, setuid files, `[build.modes]` overrides, bundled dir, extra deps and optional
`scriptlets` file, then re-run `install.sh`. Do not
publish the built RPMs anywhere; most such software isn't redistributable.

## Tests

    python3 -m unittest discover -s tests -v

Builds a fake signed apt repo (ELF binary, bundled `.so`, setuid helper, postinst)
and covers first build, no-op, bump, bad signature, sha mismatch, upstream down,
control-script warn/block/approve, soname exclusion, setuid, signing, pruning.

## Uninstall

    sudo systemctl disable --now repackd.socket repackd.service
    sudo rm -f /etc/systemd/system/repackd.{socket,service} /etc/yum.repos.d/repackd.repo /usr/local/bin/repackd
    sudo rm -rf /etc/repackd /var/lib/repackd /var/lib/private/repackd
    sudo dnf remove claude-desktop    # optional; the RPM stays installed otherwise
