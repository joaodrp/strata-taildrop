#!/bin/bash
# Install, update or remove the Taildrop provider for Strata.
#
#   curl -fsSL https://raw.githubusercontent.com/joaodrp/strata-taildrop/main/install.sh | bash
#   ./install.sh               # from a clone: installs that checkout's provider.py
#   ./install.sh --uninstall
#
# Re-running updates in place. Strata picks the change up after
# Settings -> Integrations -> Refresh, or a restart. Set PYTHON to choose the
# interpreter Strata runs the provider with.
set -euo pipefail

source_url=${STRATA_TAILDROP_URL:-https://raw.githubusercontent.com/joaodrp/strata-taildrop/main/provider.py}
providers="${XDG_CONFIG_HOME:-$HOME/.config}/strata/providers"
dir="$providers/taildrop"

die() { echo "error: $*" >&2; exit 1; }

if [[ ${1:-} == --uninstall ]]; then
    # Removes any registration with the id "taildrop", whoever installed it.
    if grep -qs '"id": "taildrop"' "$dir/provider.json"; then
        rm -rf -- "$dir"
        echo "Removed $dir. Refresh Settings -> Integrations in Strata."
    else
        echo "Nothing to remove at $dir." >&2
    fi
    exit 0
fi

# Strata runs provider commands as literal argv, so the interpreter path must
# be absolute, and the provider needs Python 3.10 or newer.
python=""
for candidate in ${PYTHON:+"$PYTHON"} /usr/bin/python3 "$(command -v python3 || true)"; do
    [[ $candidate == /* && -x $candidate ]] || continue
    if "$candidate" -I -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        python=$candidate
        break
    fi
done
[[ -n $python ]] || die "Python 3.10 or newer is required; set PYTHON to an absolute path to one."
command -v tailscale >/dev/null || echo "warning: tailscale is not installed; Taildrop will show no devices." >&2

# Strata ignores registration folders that are symlinks, owned by someone else,
# or writable by group or others. Refuse rather than install where it won't load.
check_dir() {
    local path=$1
    [[ -e $path || -L $path ]] || return 0
    [[ ! -L $path ]] || die "$path is a symlink; Strata will not load providers from it."
    [[ -d $path ]] || die "$path exists and is not a directory."
    local owner mode
    owner=$(stat -c %u -- "$path")
    mode=$(stat -c %a -- "$path")
    [[ $owner == "$(id -u)" || $owner == 0 ]] || die "$path is owned by another user; Strata will not load it."
    (( (8#$mode & 8#022) == 0 )) || die "$path is writable by group or others (mode $mode); run: chmod go-w '$path'"
}
check_dir "$providers"
check_dir "$dir"

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
# Use a checkout's provider only when this script runs from a file. Piped into
# bash there is no script file, so download rather than trust the current
# directory.
script=${BASH_SOURCE[0]:-}
here=""
if [[ -n $script && -f $script ]]; then
    here=$(cd -- "$(dirname -- "$script")" && pwd)
fi
if [[ -n $here && -f $here/provider.py ]]; then
    cp -- "$here/provider.py" "$tmp/provider.py"
else
    curl -fsSL "$source_url" -o "$tmp/provider.py"
fi
"$python" -I -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$tmp/provider.py"

[[ -d $providers ]] || install -d -m 700 -- "$providers"
install -d -m 755 -- "$dir"
install -m 644 -- "$tmp/provider.py" "$dir/provider.py"
"$python" -I - "$python" "$dir/provider.py" >"$tmp/provider.json" <<'PY'
import json, sys
print(json.dumps({"version": 1, "id": "taildrop", "name": "Taildrop", "category": "devices",
                  "command": [sys.argv[1], "-I", sys.argv[2]]}, indent=2))
PY
install -m 644 -- "$tmp/provider.json" "$dir/provider.json"
echo "Installed to $dir. In Strata: Settings -> Integrations -> Refresh."
