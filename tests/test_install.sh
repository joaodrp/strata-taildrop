#!/bin/bash
# Install, update and remove into a throwaway config directory, checking the
# files and modes Strata requires. Run from the repository root.
set -euo pipefail

home=$(mktemp -d)
trap 'rm -rf -- "$home"' EXIT
export XDG_CONFIG_HOME=$home
dir=$home/strata/providers/taildrop

fail() { echo "FAIL: $*" >&2; exit 1; }

./install.sh >/dev/null
./install.sh >/dev/null  # a second run updates in place
[[ $(stat -c %a "$home/strata/providers") == 700 ]] || fail "providers dir must be 0700"
[[ $(stat -c %a "$dir") == 755 ]] || fail "provider dir must be 0755"
for f in provider.py provider.json; do
    [[ $(stat -c %a "$dir/$f") == 644 ]] || fail "$f must be 0644"
done
cmp -s provider.py "$dir/provider.py" || fail "installed provider.py differs from the checkout"
python3 -I - "$dir" <<'PY' || fail "provider.json is wrong"
import json, os, sys
d = sys.argv[1]
m = json.load(open(os.path.join(d, "provider.json")))
assert m["id"] == "taildrop" and m["version"] == 1, m
assert os.path.isabs(m["command"][0]) and m["command"][1:] == ["-I", os.path.join(d, "provider.py")], m
PY

./install.sh --uninstall >/dev/null
[[ ! -e $dir ]] || fail "uninstall left $dir"
./install.sh --uninstall >/dev/null 2>&1 || fail "uninstall with nothing installed must succeed"

# Piped into bash, the installer downloads; it must not pick up a provider.py
# from the current directory.
repo=$PWD
decoy=$home/decoy
mkdir -p "$decoy"
echo 'print("decoy")' >"$decoy/provider.py"
cp install.sh "$decoy/install.sh"
(cd "$decoy" && STRATA_TAILDROP_URL="file://$repo/provider.py" bash <"$repo/install.sh" >/dev/null)
cmp -s provider.py "$dir/provider.py" || fail "a piped install used the current directory's provider.py"
./install.sh --uninstall >/dev/null

# An interpreter older than 3.10 is skipped for a suitable one.
old=$home/python-old
printf '#!/bin/sh\nexit 1\n' >"$old"
chmod +x "$old"
PYTHON=$old ./install.sh >/dev/null
grep -q "\"$old\"" "$dir/provider.json" && fail "an unsuitable PYTHON was used"
./install.sh --uninstall >/dev/null

# Folders Strata would refuse to load from make the install fail.
chmod 775 "$home/strata/providers"
./install.sh >/dev/null 2>&1 && fail "a group-writable providers folder was accepted"
chmod 700 "$home/strata/providers"
mv "$home/strata/providers" "$home/real-providers"
ln -s "$home/real-providers" "$home/strata/providers"
./install.sh >/dev/null 2>&1 && fail "a symlinked providers folder was accepted"
echo ok
