"""Named API connections; CLI identity and credentials have different owners."""
from dataclasses import asdict, dataclass, field
import re
from urllib.parse import urlsplit

from .errors import AgentBoxError


def valid_name(name):
    return isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name) is not None


@dataclass
class Endpoint:
    url: str = ""
    models: list[str] = field(default_factory=list)


@dataclass
class ExternalProvider:
    enabled: bool = False
    api_key: str = ""
    codex: Endpoint = field(default_factory=Endpoint)
    claude: Endpoint = field(default_factory=Endpoint)

    def to_dict(self):
        return asdict(self)


def parse_connections(data):
    if data is None:
        return {}
    def invalid(path):
        raise AgentBoxError(f"Invalid external provider configuration: {path}")
    if not isinstance(data, dict):
        invalid("external_providers must be an object")
    result = {}
    for name, raw in data.items():
        if not valid_name(name):
            invalid("provider name must match [a-z][a-z0-9_-]{0,47}")
        path = f"external_providers.{name}"
        if not isinstance(raw, dict) or set(raw) - {"enabled", "api_key", "codex", "claude"}:
            invalid(path)
        spec = ExternalProvider()
        for key, kind in (("enabled", bool), ("api_key", str)):
            value = raw.get(key, getattr(spec, key))
            if type(value) is not kind:
                invalid(f"{path}.{key}")
            setattr(spec, key, value)
        for cli in ("codex", "claude"):
            endpoint = raw.get(cli, {})
            if not isinstance(endpoint, dict) or set(endpoint) - {"url", "models"}:
                invalid(f"{path}.{cli}")
            url, models = endpoint.get("url", ""), endpoint.get("models", [])
            if not isinstance(url, str):
                invalid(f"{path}.{cli}.url")
            if url:
                try:
                    parsed = urlsplit(url)
                    valid = (parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
                             and not parsed.password and not parsed.query and not parsed.fragment
                             and not any(c.isspace() or ord(c) < 32 for c in url))
                    parsed.port
                except ValueError:
                    valid = False
                if not valid:
                    invalid(f"{path}.{cli}.url must be HTTP(S), without credentials, query or fragment")
            if (not isinstance(models, list) or any(not isinstance(m, str) or
                    re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", m) is None for m in models)):
                invalid(f"{path}.{cli}.models must contain model IDs")
            setattr(spec, cli, Endpoint(url, list(dict.fromkeys(models))))
        result[name] = spec
    return result


def validate_connections(config):
    errors = []
    for name, spec in config.external_providers.items():
        if spec.enabled and (not spec.api_key.strip() or not (spec.codex.url or spec.claude.url)):
            errors.append(f"external_providers.{name}: enabled provider requires an API key and a CLI URL")
    return errors
