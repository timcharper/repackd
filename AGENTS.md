# AGENTS.md

repackd: on-demand `.deb` -> `.rpm` repo shim for Fedora. One Python file (`repackd`), recipes in
`recipes/`, `install.sh`, tests in `tests/`. Read `README.md` first; this file only holds what the code
and README don't make obvious.

## Run

- Tests: `python3 -m unittest discover -s tests` (about 10s, no root, needs rpmbuild/rpmsign/createrepo_c/gcc/dpkg-deb).
- Try one recipe without root: README "Writing a recipe", step 5. It uses a scratch `--state` dir.

## Gotchas

- `install.sh` deletes and re-copies `/etc/repackd/recipes` from this repo's `recipes/` on every run, so a
  recipe only placed in `/etc` is lost. Custom recipes belong in `recipes/`. `--recipes` sets the whole set.
- Scriptlet files (`build.scriptlets`) go through rpm macro expansion: write `%%` for a literal `%`
  (so shell `${v%%\"}` becomes `${v%\"}`). `TestVantaScriptlet` runs the `%post` with `%%` -> `%` replaced.
- The service runs as a DynamicUser with `ProtectSystem=strict`: it writes only under `/var/lib/repackd`.
  GNU tar fails there (`openat2` is blocked), which is why `extract_tar` uses Python's `tarfile`.
- Layout: each recipe builds RPMs into `/var/lib/repackd/<recipe>/repo` (RPMs only); the one repo dnf sees is
  `/var/lib/repackd/repo`, hardlinks plus the only metadata (`Union`). `repo` is a reserved recipe name.
- `[direct]` recipes use the redirect target as the change token and read the version from the deb's
  `control` file. Do not parse versions out of URLs, and do not add a download-URL template.
- Secrets never go in recipes, tests, the RPM or the repo. Vanta's key comes from the environment or the
  root-only `/etc/repackd/vanta.env`.

## Working on this host

- Writing a file that contains `rm -rf` through a Bash heredoc trips the harness's safety check
  (`recipes/vanta.scriptlets.spec` does). Use the Write tool for such files.
- Installing, `dnf`, and `vanta-cli` need root: use the root tmux pane. Back up `/etc/vanta.conf` before
  touching the Vanta package. `vanta-cli doctor` restarts the Vanta service.
- Changes are pushed straight to `main` (no PRs).
