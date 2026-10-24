"""Prepare AUR metadata and publish only checked, released source versions."""
import argparse
from email.parser import Parser
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FILES = ('PKGBUILD', '.SRCINFO', 'LICENSE')


def settings(root=ROOT):
    data = tomllib.loads((root / 'pyproject.toml').read_text())
    return data['project'], data['tool']['agent-box']


def version_key(version, revision, epoch=0):
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version):
        raise ValueError('AUR releases require MAJOR.MINOR.PATCH versions without suffixes')
    if not re.fullmatch(r'[1-9][0-9]*', str(revision)):
        raise ValueError('pkgrel must be a positive integer')
    if not re.fullmatch(r'0|[1-9][0-9]*', str(epoch)):
        raise ValueError('epoch must be a nonnegative integer')
    return (int(epoch), *map(int, version.split('.')), int(revision))


def archive_name(version, root=ROOT):
    project, _ = settings(root)
    return project['name'].replace('-', '_') + '-' + version + '.tar.gz'


def source_url(version, root=ROOT):
    _, config = settings(root)
    return f"{config['release']['repository'].rstrip('/')}/releases/download/v{version}/{archive_name(version, root)}"


def published_digest(version, root=ROOT):
    project, _ = settings(root)
    with urllib.request.urlopen(f"https://pypi.org/pypi/{project['name']}/{version}/json", timeout=30) as response:
        files = json.load(response)['urls']
    matches = [f['digests']['sha256'] for f in files if f['filename'] == archive_name(version, root)]
    if len(matches) != 1:
        raise ValueError('Published release must contain the expected source archive')
    return matches[0]


def fetch(version, directory, root=ROOT):
    version_key(version, 1)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / archive_name(version, root)
    with urllib.request.urlopen(source_url(version, root), timeout=60) as response:
        content = response.read()
    if hashlib.sha256(content).hexdigest() != published_digest(version, root):
        raise ValueError('GitHub source archive differs from the published PyPI artifact')
    path.write_bytes(content)
    return path


def render(version, revision, digest, root=ROOT):
    project, config = settings(root)
    epoch = config['aur']['epoch']
    version_key(version, revision, epoch)
    values = {
        'MAINTAINER': config['aur']['maintainer'], 'PACKAGE': config['aur']['name'],
        'VERSION': version, 'REVISION': str(revision), 'EPOCH': str(epoch), 'SHA256': digest,
        'REPOSITORY': config['release']['repository'].rstrip('/'),
        'ARCHIVE': archive_name(version, root), 'DISTNAME': project['name'].replace('-', '_'),
    }
    if not re.fullmatch(r'[a-z0-9][a-z0-9+_.-]*', values['PACKAGE']) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('Invalid package name or source digest')
    if any('\n' in value or '\r' in value for value in values.values()):
        raise ValueError('AUR metadata must use single-line values')
    text = (root / 'packaging/aur/PKGBUILD.in').read_text()
    for name, value in values.items():
        text = text.replace('@' + name + '@', value)
    return text


def prepare(archive, version, revision, directory, root=ROOT):
    version_key(version, revision)
    project, _ = settings(root)
    prefix = archive_name(version, root).removesuffix('.tar.gz') + '/'
    with tarfile.open(archive) as source:
        info = Parser().parsestr(source.extractfile(prefix + 'PKG-INFO').read().decode())
        metadata = tomllib.loads(source.extractfile(prefix + 'pyproject.toml').read().decode())['project']
    if (info['Name'] != project['name'] or info['Version'] != version
            or metadata['name'] != project['name'] or metadata['version'] != version):
        raise ValueError('Archive name/version does not match the requested release')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'PKGBUILD').write_text(render(version, revision, digest, root))
    shutil.copyfile(root / 'packaging/aur/LICENSE', directory / 'LICENSE')
    shutil.copyfile(archive, directory / archive_name(version, root))


def srcinfo(text):
    fields = {}
    for line in text.splitlines():
        if '=' in line:
            key, value = line.strip().split('=', 1)
            fields.setdefault(key.strip(), []).append(value.strip())
    def single(key):
        values = fields.get(key, [])
        if len(values) != 1:
            raise ValueError(f'.SRCINFO must contain exactly one {key}')
        return values[0]
    fields.setdefault('epoch', ['0'])
    name, version, revision = single('pkgname'), single('pkgver'), single('pkgrel')
    if single('pkgbase') != name:
        raise ValueError('Split AUR packages are not supported')
    version_key(version, revision, single('epoch'))
    return name, version, revision, fields


def compare_candidate(candidate, existing):
    """A rerun is harmless only when the same version contains the same recipe."""
    name, version, revision, fields = srcinfo((candidate / '.SRCINFO').read_text())
    old_name, old_version, old_revision, old_fields = srcinfo((existing / '.SRCINFO').read_text())
    if name != old_name or fields.get('url') != old_fields.get('url'):
        raise ValueError('Existing AUR package has a different identity')
    new = version_key(version, revision, fields['epoch'][0])
    old = version_key(old_version, old_revision, old_fields['epoch'][0])
    if new < old:
        raise ValueError('Refusing to downgrade the AUR package')
    if new == old:
        if all((existing / f).is_file() and (candidate / f).read_bytes() == (existing / f).read_bytes() for f in FILES):
            return False
        raise ValueError('Recipe changed without an epoch/pkgver/pkgrel increase')
    return True


def publish(candidate, root=ROOT):
    _, config = settings(root)
    if set(p.name for p in candidate.iterdir()) != set(FILES) or any(not (candidate / f).is_file() or (candidate / f).is_symlink() for f in FILES):
        raise ValueError('Publish input must contain only PKGBUILD, .SRCINFO and LICENSE')
    name, version, revision, fields = srcinfo((candidate / '.SRCINFO').read_text())
    if name != config['aur']['name'] or fields.get('url') != [config['release']['repository'].rstrip('/')]:
        raise ValueError('Candidate has a different package name')
    if fields['epoch'] != [str(config['aur']['epoch'])]:
        raise ValueError('Candidate has a different AUR epoch')
    digest = published_digest(version, root)
    if fields.get('sha256sums') != [digest] or fields.get('source') != [archive_name(version, root) + '::' + source_url(version, root)]:
        raise ValueError('Candidate does not reference the verified published source')
    if (candidate / 'PKGBUILD').read_text() != render(version, revision, digest, root):
        raise ValueError('Candidate differs from the reviewed PKGBUILD template')
    if (candidate / 'LICENSE').read_bytes() != (root / 'packaging/aur/LICENSE').read_bytes():
        raise ValueError('Candidate has a different packaging license')
    with tempfile.TemporaryDirectory(prefix='agent-box-aur-push-') as directory:
        checkout = Path(directory) / 'aur'
        def git(*args, check=True):
            return subprocess.run(['git', '-C', str(checkout), *args], check=check, text=True, capture_output=True)
        subprocess.run(['git', '-c', 'init.defaultBranch=master', 'clone', f'ssh://aur@aur.archlinux.org/{name}.git', str(checkout)], check=True)
        if git('rev-parse', '--verify', 'HEAD', check=False).returncode == 0:
            if git('branch', '--show-current').stdout.strip() != 'master':
                raise ValueError('AUR checkout is not on master')
            if not compare_candidate(candidate, checkout):
                print('AUR already contains the identical checked package')
                return
        else:
            git('symbolic-ref', 'HEAD', 'refs/heads/master')
        for file in FILES:
            shutil.copyfile(candidate / file, checkout / file)
        maintainer = re.fullmatch(r'(.+) <([^<>]+)>', config['aur']['maintainer'])
        if not maintainer:
            raise ValueError('Maintainer must be Name <email>')
        git('config', 'user.name', maintainer[1])
        git('config', 'user.email', maintainer[2])
        git('add', '--', *FILES)
        git('commit', '-m', f'Update {name} to {version}-{revision}')
        # A concurrent external update causes a normal non-fast-forward refusal.
        result = git('push', 'origin', 'HEAD:master')
        print(result.stderr.strip())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('fetch', 'prepare', 'publish'))
    parser.add_argument('--version')
    parser.add_argument('--revision', default='1')
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    if args.operation == 'publish':
        publish(args.directory)
        return
    version = args.version or settings()[0]['version']
    if args.operation == 'fetch':
        print(fetch(version, args.directory))
    else:
        if not args.archive:
            parser.error('prepare requires --archive')
        prepare(args.archive, version, args.revision, args.directory)


if __name__ == '__main__':
    main()
