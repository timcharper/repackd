# repackd

On-demand `.deb` -> `.rpm` repo shim for Fedora. It tracks a vendor's Debian packaging (a **signed apt
repo**, or a plain download URL) and serves the result to `dnf` as one localhost repo, so `dnf upgrade`
keeps proprietary Debian-only packages current with no third-party rebuilder in the trust chain.

## Quick start

    ./install.sh --list                                  # recipes shipped in this repo
    sudo ./install.sh --recipes claude-desktop,vanta     # activate some (or --all)
    sudo dnf makecache --refresh --repo repackd
    sudo dnf install claude-desktop vanta

`install.sh` needs `rpm-build rpm-sign createrepo_c gnupg2 binutils tar python3 curl zstd xz`.
It installs the engine and systemd units, copies the chosen recipes into `/etc/repackd/recipes`, pins the
upstream signing key of each apt recipe (it shows the fingerprint and asks first, unless the recipe
pins one or you pass `--yes`), and writes the single dnf repo file `/etc/yum.repos.d/repackd.repo`.

## Built-in recipes

| Recipe | Source | Trust |
|---|---|---|
| `claude-desktop` | Anthropic's signed apt repo | `InRelease` verified against the pinned key; `.deb` checked against the signed sha256/size |
| `vanta` | Vanta's download redirect (no apt repo) | TLS only; the sha256 of each build is recorded in `state.json` |

### Activating and deactivating recipes

A recipe is **enabled when its `.toml` is in `/etc/repackd/recipes`**. `install.sh` manages that
directory for you:

    sudo ./install.sh --recipes vanta            # enable exactly these (others are removed from /etc/repackd/recipes)
    sudo ./install.sh --all                      # enable every recipe in this repo
    sudo ./install.sh                            # no flags: keep whatever is enabled now (first install: asks you to choose)

- Re-running is safe and is how you change the set. Pinned keys, state and built RPMs are kept.
- A deselected recipe stops being served; a package you already installed from it stays installed.
  Its state under `/var/lib/repackd/<recipe>/` is not deleted (remove it by hand to reclaim the space).
- `install.sh` rewrites `/etc/repackd/recipes` from this repo's `recipes/` directory on every run, so
  put custom recipes there too (see below) rather than only in `/etc`.

## How it works

- `repackd.socket` listens on `127.0.0.1:8742`; `repackd.service` (DynamicUser, no root) starts on the
  first connection and exits after 60s idle.
- dnf sees one repo, `repackd` (`/repo/`), holding every enabled recipe's RPMs with one set of metadata.
- A request for `/repo/repodata/repomd.xml` syncs every enabled recipe in parallel, each at most once
  per 30 min (`REPACKD_CHECK_MINUTES`). A recipe failing (upstream down, bad signature, build error)
  leaves its old RPMs served, logs to the journal and does not block the others.
- Each recipe builds into `/var/lib/repackd/<recipe>/repo`; `/var/lib/repackd/repo` hardlinks them together.
- A new version is unpacked, a spec is generated, `rpmbuild` runs, the RPM is signed with a local key
  (`/var/lib/repackd/RPM-GPG-KEY-repackd`, trusted by the `.repo` file), and `createrepo_c` updates the metadata.

**apt recipes:** `InRelease` is verified with `gpgv` against the key pinned in `/etc/repackd/keys`;
`Packages` and the `.deb` are verified against the signed SHA256/size chain.

**direct recipes** (no apt repo, no signatures): a `HEAD` on the recipe's `url`, redirects not followed,
yields a redirect to the `.deb`. A redirect target that was already built, or is already blocked, is
"no update" and nothing is downloaded. A new target is downloaded (TLS is the only trust), and the
version comes from the deb's own `control` file. A failed build does not record the target, so it is
retried. The redirect must be a single hop and may not downgrade https to http.

**Version mapping:** `[N:]upstream[-rev]` -> `Epoch: N`, `Version: upstream`, `Release: <rev|1>.repackd`.

**Maintainer scripts** (`pre/postinst`, `pre/postrm`) are not converted. Their hashes are tracked; if
upstream changes them you get a journal warning and a diff in `/var/lib/repackd/<recipe>/diffs/`.
`control_change = "block"` (recipe) or `REPACKD_CONTROL_CHANGE=block` refuses the update until you review
it and run `repackd approve <recipe>`. A recipe can supply its own scriptlets instead (see `scriptlets`).

## Writing a recipe

A recipe is one TOML file in this repo's `recipes/` directory, named after the recipe (`recipes/foo.toml`
-> `foo`; lowercase letters, digits, `.`, `_`, `-`; `repo` is reserved). Steps:

1. **Pick the source.** A vendor with a signed apt repo gets `[apt]` (preferred: you verify a signature).
   A vendor with only a download URL that redirects to a `.deb` gets `[direct]`.
2. **Copy a starting point:** `recipes/claude-desktop.toml` (apt) or `recipes/vanta.toml` (direct).
3. **Fill in the source** (see the reference). For apt you also need the vendor's key URL and, ideally,
   its fingerprint from the vendor's own docs.
4. **Look at the deb** to decide what to strip or protect: Debian-only paths (`etc/apt`, apparmor,
   keyrings), setuid helpers, and the directory that holds bundled `.so` files and binaries.
5. **Test without root** against a scratch state directory, with only your recipe in it:

       mkdir -p /tmp/rd/{recipes,keys,state}
       cp recipes/foo.toml /tmp/rd/recipes/            # plus its scriptlets file, if any
       # apt recipes only: the dearmored vendor key, named as in `keyring`
       curl -fsSL https://vendor.example/key.asc | gpg --dearmor > /tmp/rd/keys/foo.gpg
       ./repackd --state /tmp/rd/state --recipes /tmp/rd/recipes --keys /tmp/rd/keys sync foo
       rpm -qplv /tmp/rd/state/foo/repo/*.rpm; rpm -qp --requires /tmp/rd/state/foo/repo/*.rpm

   Check the file list, modes, `Requires` (a bundled library must not appear) and `--scripts`.
6. **Activate it:** `sudo ./install.sh --recipes foo` (add it to your existing list, since `--recipes`
   sets the whole set). For apt recipes this also pins the key.
7. **Add tests** to `tests/test_repackd.py` if the recipe needs engine features that are not covered yet.

Do not publish the built RPMs anywhere; most such software isn't redistributable.

### Recipe reference

Top level:

| Key | Meaning |
|---|---|
| `name` | Recipe name (required). |
| `summary`, `license`, `url` | RPM metadata. `license` defaults to `Proprietary`. |

Exactly one source table:

| Key | Meaning |
|---|---|
| `[apt] url`, `package` | apt repo base URL and the package to track (required). |
| `[apt] suite`, `component` | Default `stable` and `main`. |
| `[apt] keyring` | File name of the dearmored key in `/etc/repackd/keys` (required). |
| `[apt] key_url` | Where `install.sh` downloads the vendor key from. |
| `[apt] key_fingerprint` | If set, `install.sh` refuses a key that doesn't match. Empty means trust on first use, with a prompt. |
| `[direct] url` | URL that redirects once to the `.deb` (required). |
| `[direct] package` | Must equal the deb's `control` `Package`; defaults to the recipe name. |

`[build]` (all optional):

| Key | Meaning |
|---|---|
| `strip` | Paths inside the deb to drop (Debian-only files). |
| `setuid` | Files installed `4755 root:root`. |
| `bundled_dir` | Directory of vendor-bundled libraries: its `.so` files are not exported as Provides and not required from the system. The directory itself is owned by the RPM. |
| `owned_dirs` | Directories the RPM owns; defaults to `bundled_dir`. |
| `requires`, `recommends`, `suggests` | Extra RPM dependencies. |
| `exclude_requires` | Extra regex alternatives for `__requires_exclude`, if automatic dependency detection misfires. |
| `control_change` | `warn` (default) or `block` when upstream's maintainer scripts change. |
| `keep` | Newest RPMs retained for rollback (default 3). |
| `scriptlets` | File next to the recipe holding `%pre/%post/%preun/%postun`, used instead of the deb's maintainer scripts. `install.sh` copies it with the recipe. Write `%%` for a literal `%`. |
| `[build.modes]` | Table of `"path/in/deb" = "0600"` mode overrides. Files under `etc/` are `%config(noreplace)`. |

## Claude Desktop notes

Compare the fingerprint `install.sh` prints with Anthropic's docs. The recipe pins `key_fingerprint`
so a different key is refused. Cowork wants KVM/QEMU (`qemu-kvm` is a Recommends) plus the one-time
`kvm` group step from the docs.

## Vanta notes

    sudo dnf install vanta                                     # installs unregistered
    sudo /var/vanta/vanta-cli register --secret=<key> --email=<email>

To register at install time, put the values in a root-only file outside any repo before the first install:

    sudo install -m600 /dev/null /etc/repackd/vanta.env
    sudoedit /etc/repackd/vanta.env       # VANTA_KEY=...  VANTA_OWNER_EMAIL=...  VANTA_REGION=us  (plain KEY=value lines)

It is read only on a fresh install (never on upgrade), parsed rather than sourced, and ignored unless it
is owned by root with mode 0600 or 0400. Variables in the environment win
(`sudo env VANTA_KEY=... VANTA_OWNER_EMAIL=... dnf install vanta`, which lands in shell history).
`VANTA_NOSTART=1` skips the start. The file may be deleted once the host is registered.

No secret is ever in a recipe, the RPM or this repo; `install.sh` never touches `/etc/repackd/vanta.env`.
The recipe's scriptlets replace the vendor maintainer scripts (those sniff sysvinit/upstart); the vendor
scripts stay hash-tracked, so a change still gets a warning and a diff. `/etc/vanta.conf` is
`%config(noreplace)`: an upgrade never overwrites a registered host's config (a new default may appear as
`/etc/vanta.conf.rpmnew`). x86_64 only; the download endpoint has no arm64 path.

## Bounded storage

Nothing grows without limit: only the newest `keep` RPMs per recipe (default 3) are retained (older ones
are deleted on each successful build, which is also your rollback window); the merged repo drops links to
deleted RPMs and rewrites its metadata without keeping old copies; the download/extract work dir is
removed after every sync (and on startup after a crash); control-script diffs are capped at 5. The
`.deb` is never kept. A deselected recipe's state directory is left alone.

## Tests

    python3 -m unittest discover -s tests -v

Needs `rpmbuild`, `rpmsign`, `createrepo_c`, `gpg`, `gcc` and `dpkg-deb`. Builds a fake signed apt repo
(ELF binary, bundled `.so`, setuid helper, postinst) and covers first build, no-op, bump, bad signature,
sha mismatch, upstream down, control-script warn/block/approve, soname exclusion, setuid, signing,
pruning and the merged repo; a fake redirecting download server for the direct source (unchanged
redirect, new target, broken build retried, package mismatch, scriptlets, mode overrides); and the
Vanta `%post` against a sandbox.

## Uninstall

    sudo systemctl disable --now repackd.socket repackd.service
    sudo rm -f /etc/systemd/system/repackd.{socket,service} /etc/yum.repos.d/repackd.repo /usr/local/bin/repackd
    sudo rm -rf /etc/repackd /var/lib/repackd /var/lib/private/repackd
    sudo dnf remove claude-desktop vanta    # optional; the RPMs stay installed otherwise
