#!/usr/bin/env bash
# Run only inside a disposable Arch container; never mount the user's home.
set -euo pipefail
mode=${1:?usage: check_arch.sh candidate|release OUTPUT [VERSION] [PKGREL]}
output=${2:?output directory required}
version=${3:-}
revision=${4:-1}
case "$mode" in candidate|release) ;; *) exit 2 ;; esac
if [[ $(id -u) != 0 || ! -e /run/.containerenv && ! -e /.dockerenv ]]; then
  echo 'Run this check inside a disposable Arch container' >&2
  exit 1
fi
pacman -Syu --noconfirm --needed base-devel python python-build python-installer python-setuptools python-wheel podman git
useradd --create-home builder
mkdir -p /work/repo "$output"
# Include pending source edits for local checks, but exclude ignored caches and secrets.
(cd /repo && git -c safe.directory=/repo ls-files --cached --others --exclude-standard -z |
  tar --null -T - -cf -) | tar -xf - -C /work/repo
if [[ "$mode" == candidate ]]; then
  (cd /work/repo && python -m build --sdist --no-isolation --outdir /work/dist)
else
  python /work/repo/tools/aur.py fetch --version "$version" --directory /work/dist
fi
archives=(/work/dist/*.tar.gz)
[[ ${#archives[@]} == 1 ]]
args=(prepare --archive "${archives[0]}" --revision "$revision" --directory /work/package)
[[ -z "$version" ]] || args+=(--version "$version")
python /work/repo/tools/aur.py "${args[@]}"
chown -R builder:builder /work
runuser -u builder -- bash -c 'cd /work/package && makepkg --cleanbuild --noconfirm && makepkg --printsrcinfo > .SRCINFO'
# Development tests are a CI gate, never part of an AUR user's makepkg run.
test_runners=(/work/package/src/*/tests/run_suite.py)
[[ ${#test_runners[@]} == 1 ]]
echo 'Running the source test suite in disposable Arch CI'
runuser -u builder -- python "${test_runners[0]}"
packages=(/work/package/*.pkg.tar.zst)
[[ ${#packages[@]} == 1 ]]
pacman -U --noconfirm "${packages[0]}"
cd /tmp
# Installer ownership and resource lookup must work outside the source tree.
runuser -u builder -- /usr/bin/agent-box selftest
python - <<'PY'
from importlib.resources import files
from importlib.metadata import version
assert files('agent_box').joinpath('Containerfile').is_file()
assert files('agent_box').joinpath('skills/ab-work/SKILL.md').is_file()
print('Installed system package:', version('agent-box-tool'))
PY
runuser -u builder -- bash -c 'mkdir -p ~/.config/agent-box; printf "preserve-me\n" > ~/.config/agent-box/aur-install-sentinel'
pacman -U --noconfirm "${packages[0]}"
runuser -u builder -- /usr/bin/agent-box selftest
package_name=$(python -c 'import tomllib; print(tomllib.load(open("/work/repo/pyproject.toml", "rb"))["tool"]["agent-box"]["aur"]["name"])')
pacman -R --noconfirm "$package_name"
[[ ! -e /usr/bin/agent-box ]]
[[ $(cat /home/builder/.config/agent-box/aur-install-sentinel) == preserve-me ]]
mkdir -p "$output/metadata" "$output/packages"
cp /work/package/PKGBUILD /work/package/.SRCINFO /work/package/LICENSE "$output/metadata/"
cp "${packages[0]}" "$output/packages/"
echo 'Arch package build, install, reinstall, removal and state preservation passed'
