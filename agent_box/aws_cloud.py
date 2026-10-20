"""Selected AWS profiles and their authentication closure, copied without running AWS.

AWS owns the INI and token formats. Keep config values intact, but omit profiles
whose host-bound authentication cannot work here rather than falling through to
some unrelated credential provider. Only cache names derived from selected
sessions are read; a directory scan would expose other accounts' sign-ins.
"""
from __future__ import annotations

import configparser
import hashlib
import io
import json
import os
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import cloud
from .paths import AWS_CONFIG_DIR

_CREDENTIAL_KEYS = frozenset(('aws_access_key_id', 'aws_secret_access_key', 'aws_session_token',
                              'aws_security_token', 'aws_account_id'))
_UNSUPPORTED = ('credential_process', 'credential_source', 'web_identity_token_file', 'mfa_serial')


def _ini() -> configparser.RawConfigParser:
    # AWS section names are literal; [DEFAULT] must not inject another account into every profile.
    return configparser.RawConfigParser(default_section='\x00', interpolation=None)


def _read_ini(path: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    parser = _ini()
    try:
        parser.read_string(path.read_text(encoding='utf-8'))
        return {s: dict(parser[s]) for s in parser.sections()}, []
    except FileNotFoundError:
        return {}, []
    except (OSError, UnicodeError, configparser.Error) as exc:
        # Parser messages can contain the offending line, including credentials.
        return {}, [f'AWS {path.name}: unreadable ({type(exc).__name__}); not forwarded']


@dataclass
class Profiles:
    config: dict[str, dict[str, str]] = field(default_factory=dict)
    credentials: dict[str, dict[str, str]] = field(default_factory=dict)
    sections: dict[str, dict[str, str]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return list(dict.fromkeys([*self.config, *self.credentials]))

    def values(self, name: str) -> dict[str, str]:
        return {**self.config.get(name, {}), **self.credentials.get(name, {})}


def read() -> Profiles:
    sections, errors = _read_ini(AWS_CONFIG_DIR / 'config')
    credentials, credential_errors = _read_ini(AWS_CONFIG_DIR / 'credentials')
    profiles, normalized = {}, {}
    for section, values in sections.items():
        if section == 'default':
            profiles['default'] = values
        elif section.startswith(('profile ', 'sso-session ', 'services ')):
            try:
                parts = shlex.split(section)
            except ValueError:
                continue
            if len(parts) == 2:
                if parts[0] == 'profile':
                    profiles[parts[1]] = values
                else:
                    normalized[' '.join(parts)] = values
    return Profiles(profiles, {s: {k: v for k, v in values.items() if k in _CREDENTIAL_KEYS}
                               for s, values in credentials.items()}, normalized, errors + credential_errors)


def auth(values: dict[str, str]) -> str:
    if any(k in values for k in _UNSUPPORTED):
        return 'unsupported: ' + ', '.join(k for k in _UNSUPPORTED if k in values)
    if 'role_arn' in values:
        return 'role (source_profile)'
    if values.get('aws_access_key_id'):
        return 'session credentials' if values.get('aws_session_token') or values.get('aws_security_token') else 'static keys'
    if 'login_session' in values:
        return 'aws login'
    if 'sso_session' in values or 'sso_start_url' in values:
        return 'SSO session' if 'sso_session' in values else 'legacy SSO'
    return 'no file credentials'


def closure(found: Profiles, selected: list[str]) -> tuple[list[str], list[str]]:
    """A broken role chain never forwards its partial sources as a usable selection."""
    result, problems = [], []

    def visit(name: str, trail: tuple[str, ...]) -> list[str]:
        if name not in found.names:
            raise ValueError(f'profile {name} is missing')
        if name in trail:
            raise ValueError('source_profile cycle')
        values = found.values(name)
        # Rendering preserves referenced sections even when static credentials take precedence.
        if 'sso_session' in values and f"sso-session {values['sso_session']}" not in found.sections:
            raise ValueError(f'profile {name} has missing SSO session settings')
        unsupported = [k for k in _UNSUPPORTED if k in values]
        if unsupported:
            raise ValueError(f'profile {name} uses unsupported ' + ', '.join(unsupported))
        chain = [name]
        if 'role_arn' in values:
            source = values.get('source_profile', '')
            if not source:
                raise ValueError(f'profile {name} has no source_profile')
            # AWS permits a self-reference when the same profile has static credentials.
            if source != name or not (values.get('aws_access_key_id') and values.get('aws_secret_access_key')):
                chain += visit(source, (*trail, name))
        elif values.get('aws_access_key_id'):
            if not values.get('aws_secret_access_key'):
                raise ValueError(f'profile {name} has incomplete static credentials')
        elif 'login_session' in values:
            if not values['login_session']:
                raise ValueError(f'profile {name} has an empty login_session')
        elif 'sso_session' in values or 'sso_start_url' in values:
            session = values.get('sso_session')
            settings = found.sections.get(f'sso-session {session}', {}) if session else values
            if not all(settings.get(k) for k in ('sso_start_url', 'sso_region')):
                raise ValueError(f'profile {name} has missing SSO session settings')
            if not all(values.get(k) for k in ('sso_account_id', 'sso_role_name')):
                raise ValueError(f'profile {name} has no SSO account/role credentials')
            if session and any(k in values and values[k] != settings.get(k)
                               for k in ('sso_start_url', 'sso_region')):
                raise ValueError(f'profile {name} has conflicting SSO session settings')
        else:
            raise ValueError(f'profile {name} has no supported file credentials')
        if values.get('services') and f"services {values['services']}" not in found.sections:
            raise ValueError(f'profile {name} has missing services settings')
        return chain

    for name in selected:
        try:
            result += visit(name, ())
        except ValueError as exc:
            problems.append(f'AWS profile {name}: {exc}; not forwarded')
    return list(dict.fromkeys(result)), problems


def choices(chosen: list[str]) -> tuple[list[str], list[str], list[str], list[str]]:
    found = read()
    names = found.names + [n for n in chosen if n not in found.names]
    details, marks = [], []
    for name in names:
        values = found.values(name)
        label = auth(values) if name in found.names else 'missing'
        selected, warnings = closure(found, [name])
        lines = [f'AWS profile {name}', f'Sign-in: {label}']
        sources = [n for n in selected if n != name]
        if sources:
            lines.append('Also forwards source profiles with their full account access: ' + ', '.join(sources))
        if any('sso_session' in found.values(n) or 'sso_start_url' in found.values(n) for n in selected):
            lines.append('The shared SSO token grants every account/role allowed by that session, not only this profile. '
                         'Its refresh material is copied too.')
        if any('login_session' in found.values(n) for n in selected):
            lines.append('Copies the login session, refresh token and DPoP private key; all session access is exposed.')
        details.append('\n'.join([*lines, *warnings]))
        marks.append(label)
    return names, details, marks, found.problems


def runtime_dir(workspace_id: str) -> Path:
    return cloud.runtime_dir(workspace_id).with_name('aws')


def _encode(sections: dict[str, dict[str, str]]) -> bytes:
    parser, output = _ini(), io.StringIO()
    parser.read_dict(sections)
    parser.write(output)
    return output.getvalue().encode('utf-8')


def _expired(value: str) -> bool:
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00').replace('UTC', '+00:00'))
        return stamp.astimezone(timezone.utc) <= datetime.now(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return True


def _cache(kind: str, key: str, values: dict[str, str]) -> tuple[str, bytes | None, str]:
    # These are AWS CLI v2's SSOTokenLoader and generate_login_cache_key conventions.
    digest = hashlib.sha1(key.encode()).hexdigest() if kind == 'sso' else hashlib.sha256(key.encode()).hexdigest()
    filename = digest + '.json'
    try:
        data = (AWS_CONFIG_DIR / kind / 'cache' / filename).read_bytes()
        token = json.loads(data)
        if not isinstance(token, dict):
            raise ValueError()
        if kind == 'sso':
            if not all(token.get(k) for k in ('accessToken', 'expiresAt')):
                raise ValueError()
            if any(token.get(k) != values.get(v) for k, v in (('startUrl', 'sso_start_url'), ('region', 'sso_region'))):
                raise ValueError()
            expiry = token['expiresAt']
        else:
            if not all(token.get(k) for k in ('accessToken', 'refreshToken', 'dpopKey', 'clientId')):
                raise ValueError()
            access = token['accessToken']
            if not isinstance(access, dict) or not all(access.get(k) for k in
                    ('accessKeyId', 'secretAccessKey', 'sessionToken', 'expiresAt', 'accountId')):
                raise ValueError()
            expiry = access['expiresAt']
        warning = 'cached token expired or expiry unreadable; CLI refresh or interactive login is required' if _expired(expiry) else ''
        return filename, data, warning
    except FileNotFoundError:
        return filename, None, 'cache missing; sign in on the host and relaunch, or sign in inside the box'
    except (OSError, ValueError, TypeError):
        return filename, None, 'cache unreadable or invalid; not forwarded, sign in again'


def sync(config, workspace_id: str) -> list[str]:
    selected = config.credentials.cloud.aws if config.credentials.cloud.enabled else []
    directory = runtime_dir(workspace_id)
    if not selected and not directory.parent.exists():
        return []
    # Only the owned top-level runtime root is trusted; every container-writable component is opened no-follow.
    cloud.CLOUD_RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = cloud._directory(cloud.CLOUD_RUNTIME_DIR)
    opened = [root]
    try:
        for name in ('workspaces', workspace_id):
            root = cloud._directory(name, root)
            opened.append(root)
        if not selected:
            cloud._remove(root, 'aws')
            return []
        root = cloud._directory('aws', root)
        opened.append(root)
        found = read()
        forwarded, warnings = closure(found, selected)
        warnings = found.problems + warnings
        sections, credentials, caches = {}, {}, {'sso': {}, 'login': {}}
        for name in forwarded:
            values = found.values(name)
            sections['default' if name == 'default' else 'profile ' + shlex.quote(name)] = found.config.get(name, {})
            if name in found.credentials:
                credentials[name] = found.credentials[name]
            for key, prefix in (('sso_session', 'sso-session'), ('services', 'services')):
                if values.get(key):
                    section = f'{prefix} {values[key]}'
                    sections[f'{prefix} {shlex.quote(values[key])}'] = found.sections[section]
            if values.get('ca_bundle'):
                warnings.append(f'AWS profile {name}: ca_bundle refers to a host file; it is not copied')
            kind, cache_key, settings = '', '', values
            if values.get('login_session'):
                kind, cache_key = 'login', values['login_session']
            elif values.get('sso_session') or values.get('sso_start_url'):
                kind = 'sso'
                cache_key = values.get('sso_session', values.get('sso_start_url', ''))
                if values.get('sso_session'):
                    settings = found.sections[f"sso-session {values['sso_session']}"]
            if kind:
                filename, data, warning = _cache(kind, cache_key, settings)
                if data is not None:
                    caches[kind][filename] = data
                if warning:
                    warnings.append(f'AWS profile {name}: {kind} {warning}')
        cloud._write(root, 'config', _encode(sections))
        cloud._write(root, 'credentials', _encode(credentials))
        # Derived role credentials must not survive removed sources; the CLI can mint them again.
        for kind in ('sso', 'login', 'cli'):
            parent = cloud._directory(kind, root)
            try:
                cache = cloud._directory('cache', parent)
                try:
                    wanted = caches.get(kind, {})
                    for filename in os.listdir(cache):
                        if filename not in wanted:
                            cloud._remove(cache, filename)
                    for filename, data in wanted.items():
                        cloud._write(cache, filename, data)
                finally:
                    os.close(cache)
            finally:
                os.close(parent)
        return list(dict.fromkeys(warnings))
    finally:
        for fd in reversed(opened):
            os.close(fd)
