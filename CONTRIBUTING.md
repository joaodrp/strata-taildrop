# Contributing

Bug reports and pull requests are welcome. Read [How it works](docs/how-it-works.md) first:
it covers the protocol and the checks every change must keep.

If a device never shows up or a send fails, include what `tailscale file cp --targets` lists, your
Strata version (`strata --version`), and the notification's text.

## Layout

| File | |
| --- | --- |
| `provider.py` | Everything: the protocol loop, peer discovery, tickets, activation checks and the detached sender |
| `install.sh` | Copies `provider.py` into Strata's providers folder and writes `provider.json` |
| `tests/test_provider.py` | Unit tests, standard library only |
| `tests/test_install.sh` | Installs, updates and removes into a throwaway config folder |
| [`docs/how-it-works.md`](docs/how-it-works.md) | The protocol, the checks, and why the sender reads open files |
| `docs/images/` | README screenshots; see [Screenshots](#screenshots) |
| `.github/`, `release-please-config.json`, `.release-please-manifest.json` | CI and the release automation; see [Releases](#releases) |

## Running it

Install your checkout into Strata, then select **Refresh** in **Settings -> Integrations**:

```sh
./install.sh
```

Run it again after every change, then Refresh. The installer copies the file, so editing the
checkout alone changes nothing; Strata also refuses symlinked registrations.

To talk to the provider without Strata, pipe a request into it:

```sh
echo '{"version":1,"id":1,"method":"menu","paths":["'"$HOME"'/somefile"],"background":false}' \
  | python3 -I provider.py
```

Strata discards provider stderr, so debug output appears only when you run it by hand like this.

## Checks

```sh
python3 -I -B -m unittest discover -s tests -v
tests/test_install.sh
shellcheck install.sh tests/test_install.sh
```

CI runs all three. The tests use fake `tailscale` and notifier scripts, so they never touch the
network or a real device. The installer test points `STRATA_TAILDROP_URL` at the checkout to
exercise the piped `curl | bash` path without downloading.

Coverage, with the standard library's `trace` (no `coverage` install needed):

```sh
python3 -I -B -m trace --count --missing --summary --coverdir=/tmp/cover \
  --module unittest discover -s tests | grep provider
grep -n '>>>>>>' /tmp/cover/*provider.cover    # lines no test ran
```

The one-line `__main__` call shows as uncovered, since only a subprocess test reaches it;
`cli()`, which it calls, is tested directly. Keep everything else covered.

The tests prove the logic, not that a file reaches a phone. Before a release, send a
real file to a real device from Strata, and check that the notification arrives.

## Screenshots

The README pictures must not show real device names, drives, paths or tailnet names. Shoot them
from a demo Strata instead of your own:

- Give it its own home with a sample folder, and symlink `~/.local/state/omarchy` into that home
  so it follows your Omarchy theme. Strata reads the theme from that literal path.
- Run it under `dbus-run-session` so it does not hand off to your running Strata, with
  `HOME` and `XDG_CONFIG_HOME` pointing at the demo.
- Register a stand-in provider that wraps `provider.py` with made-up devices (laptop, phone,
  tablet) and a fake send.
- Collapse the sidebar (`Ctrl+B`) so mounted drives show as icons only.
- The demo's private D-Bus session swallows notifications, so shoot the notification by sending
  the same text on your real session, on an empty workspace so only wallpaper shows around it.

Capture at native scale with `grim -g`, crop, and run `optipng -o2`. Show images at half their
pixel width in the README on a 2x display.

## Conventions

Standard library only, one file. A provider is something people install by copying, so a
dependency is a cost every user pays.

Comments carry what the code cannot: why an obvious alternative was rejected. They describe the
current state, never the change; git history holds that.

## Gotchas

- **Edits need a Refresh.** Strata starts providers at launch and on **Refresh**; an edited
  `provider.py` runs only after one.
- **Registration files must not be group or world writable**, and cannot be symlinks. The
  installer sets `0700` on the providers folder, `0755` on the provider's folder and `0644` on
  its files.
- **Never act on a path at send time.** Paths can change between the menu and the click; send
  only from the descriptors opened and checked during activation. See
  [docs/how-it-works.md](docs/how-it-works.md).
- **Sending needs permission to talk to `tailscaled`.** With the usual root daemon that means
  the operator set for your user, or every send fails with a permission error.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/), one logical change each. Describe
what the change does and why, in the body, with backticks around identifiers.

## Releases

[release-please](https://github.com/googleapis/release-please) reads the Conventional Commits on
`main` and keeps a release pull request open with the next version and the changelog. Merging
it bumps `__version__` in `provider.py`, tags `vX.Y.Z` and publishes the GitHub release, then
checks that the tag and `__version__` agree. `feat` bumps the minor version, `fix` the patch, a
`!` or `BREAKING CHANGE` footer the major.

## Scope

Sending files from Strata to a tailnet device. Receiving Taildrops (`tailscale file get`) is a
different job with its own questions (where files land, when to fetch) and is out of scope for
now.
