"""The small project subset of Config; credentials and host policy stay global."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .atomicio import atomic_write_text
from .config import Config, TRACKER_PROVIDERS
from .errors import AgentBoxError
from .repo_binding import BINDING_FILE, repository_root, server_identity


def _fields(value):
    if isinstance(value, dict):
        return {key: _fields(item) for key, item in value.items()}
    return type(value)


PROJECT_FIELDS = {
    "workflow": {key: _fields(value) for key, value in Config().workflow.to_dict().items() if key != "enabled"},
    "browser": {"enabled": bool},
    "image": _fields(Config().image.to_dict()),
    "tracker": {"provider": str, "project": str, "recursive": bool,
                "connections": {provider: {"url": str} for provider in TRACKER_PROVIDERS}},
}
CREDENTIAL_FIELDS = {("notifications", "telegram_token"), ("github", "gh_token"),
                     *(("tracker", "connections", provider, "token") for provider in TRACKER_PROVIDERS)}


def project_field(path):
    rule = PROJECT_FIELDS
    for part in path:
        if not isinstance(rule, dict) or part not in rule:
            return None
        rule = rule[part]
    return rule


def merge(base, overrides):
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        result[key] = merge(result.get(key, {}), value) if isinstance(value, dict) else copy.deepcopy(value)
    return result


def validate(data, path):
    def credentials(value, parts=()):
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            full = (*parts, key)
            if full in CREDENTIAL_FIELDS or (len(full) == 4 and full[:2] == ("tracker", "connections") and full[-1] == "token"):
                raise AgentBoxError(f"PROJECT_CREDENTIALS_FORBIDDEN: {path}: {'.'.join(full)}; stop, notify the user, and do not commit this file")
            credentials(item, full)

    def fields(value, rule, parts=()):
        dotted = '.'.join(parts) or '<root>'
        if isinstance(rule, dict):
            if not isinstance(value, dict):
                raise AgentBoxError(f"Invalid project configuration: {path}: {dotted} must be an object")
            for key, item in value.items():
                if key not in rule:
                    raise AgentBoxError(f"Invalid project configuration: {path}: {'.'.join((*parts, key))} is not a project field")
                fields(item, rule[key], (*parts, key))
        elif type(value) is not rule and not (rule in (int, float) and type(value) in (int, float)):
            raise AgentBoxError(f"Invalid project configuration: {path}: {dotted} has an invalid type")
        elif parts and parts[-1] == 'effort' and not value:
            raise AgentBoxError(f"Invalid project configuration: {path}: {dotted} must be nonempty")
        elif rule is list and any(not isinstance(item, str) for item in value):
            raise AgentBoxError(f"Invalid project configuration: {path}: {dotted} must contain strings")
    credentials(data)
    fields(data, PROJECT_FIELDS)
    tracker = data.get('tracker', {})
    if 'provider' in tracker and tracker['provider'] not in TRACKER_PROVIDERS:
        raise AgentBoxError(f"Invalid project configuration: {path}: tracker.provider is unsupported")
    if 'project' in tracker and not tracker['project']:
        raise AgentBoxError(f"Invalid project configuration: {path}: tracker.project must be nonempty")
    for provider, connection in tracker.get('connections', {}).items():
        if 'url' in connection:
            try:
                server_identity(connection['url'])
            except AgentBoxError:
                raise AgentBoxError(f"Invalid project configuration: {path}: tracker.connections.{provider}.url must be an HTTP(S) URL without credentials, query, or fragment") from None
    workflow = data.get("workflow", {})
    for key in ("merge_mode", "routing"):
        if key in workflow and not workflow[key]:
            raise AgentBoxError(f"Invalid project configuration: {path}: workflow.{key} must be nonempty")
    errors = Config.from_dict(merge(Config().to_dict(), data)).workflow.validate()
    if errors:
        raise AgentBoxError(f"Invalid project configuration: {path}: {errors[0]}")


def project_path(cwd=None):
    try:
        return repository_root(cwd) / BINDING_FILE
    except AgentBoxError:
        return (Path.cwd() if cwd is None else Path(cwd)) / BINDING_FILE


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate project field")
        result[key] = value
    return result


def load(path=None):
    path = project_path() if path is None else Path(path)
    if path.is_symlink():
        raise AgentBoxError(f"Refusing symlink: {path}")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(), object_pairs_hook=_unique_object)
    except (OSError, ValueError):
        raise AgentBoxError(f"Invalid project configuration: {path}: cannot read JSON object") from None
    validate(data, path)
    return data


def save(path, data):
    validate(data, path)
    if path.is_symlink():
        raise AgentBoxError(f"Refusing symlink: {path}")
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + '\n',
                      path.stat().st_mode & 0o777 if path.exists() else 0o644)


def apply(config, data):
    result = Config.from_dict(merge(config.to_dict(), data))
    # Preserve the selected account's address before overlaying project identity.
    result._project_base = getattr(config, '_project_base', config)
    errors = result.workflow.validate()
    if errors:
        raise AgentBoxError(errors[0])
    return result
