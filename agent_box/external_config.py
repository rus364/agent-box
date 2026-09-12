"""Named API connections; CLI identity and credentials have different owners."""
from dataclasses import asdict, dataclass, field
import json
import re
from urllib.parse import urlsplit

from .errors import AgentBoxError

EFFORTS = ('default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
API_EFFORTS = {'codex': EFFORTS, 'claude': ('default', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')}


def valid_model(model, cli):
    suffix = r'(?:\[1m\])?' if cli == 'claude' else ''
    return isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*' + suffix, model) is not None


def model_defaults(cli):
    values = dict(display_name='', effort='default', supported_efforts=[], context_window=0,
                  auto_compact_token_limit=0, env={})
    if cli == 'codex':
        values.update(input_modalities=['text'], supports_parallel_tool_calls=False,
                      supports_reasoning_summaries=False, metadata={})
    return values


def parse_env(value, path):
    # API identity and integration paths belong to the launcher, not model tuning.
    reserved = {'HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'CLAUDECODE',
                'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_ORG_ID', 'OPENAI_ORGANIZATION',
                'OPENAI_PROJECT_ID', 'CODEX_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
                'ANTHROPIC_BASE_URL', 'ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_MODEL'}
    if not isinstance(value, dict):
        raise AgentBoxError(f'Invalid external provider configuration: {path} must be an environment object')
    for key, item in value.items():
        if (not isinstance(key, str) or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) is None
                or not isinstance(item, str) or '\0' in item or key in reserved
                or key.startswith(('AGENT_BOX_', 'ANTHROPIC_DEFAULT_', 'CLAUDE_CODE_OAUTH_',
                                   'CLAUDE_CODE_USE_', 'CLAUDE_CODE_API_KEY_'))
                or 'REMOTE_CONTROL' in key):
            raise AgentBoxError(f'Invalid external provider configuration: {path}; invalid or launcher-owned variable')
    return dict(value)


def parse_models(raw, cli, path):
    if not isinstance(raw, dict):
        raise AgentBoxError(f'Invalid external provider configuration: {path} must map model IDs to settings')
    result = {}
    for model, values in raw.items():
        defaults = model_defaults(cli)
        def invalid():
            raise AgentBoxError(f'Invalid external provider configuration: {path}; invalid model settings')
        if not valid_model(model, cli) or not isinstance(values, dict) or set(values) - set(defaults):
            invalid()
        spec = {**defaults, **values}
        for key, default in defaults.items():
            if type(spec[key]) is not type(default):
                invalid()
        efforts = spec['supported_efforts']
        allowed = API_EFFORTS[cli]
        if (spec['effort'] not in allowed or any(not isinstance(e, str) or e not in allowed[1:] for e in efforts)
                or len(set(efforts)) != len(efforts)
                or efforts and spec['effort'] != 'default' and spec['effort'] not in efforts):
            invalid()
        context, compact = spec['context_window'], spec['auto_compact_token_limit']
        if context < 0 or compact < 0 or context and compact >= context:
            invalid()
        if cli == 'claude' and (context not in (0, 200000, 1048576) or model.endswith('[1m]') and context == 200000):
            invalid()
        spec['env'] = parse_env(spec['env'], path + '.env')
        if cli == 'codex':
            modalities = spec['input_modalities']
            if not modalities or any(m not in ('text', 'image') for m in modalities) or len(set(modalities)) != len(modalities):
                invalid()
            # Typed fields have one owner; advanced native metadata fills other slots.
            owned = {'slug', 'display_name', 'default_reasoning_level', 'supported_reasoning_levels',
                     'context_window', 'auto_compact_token_limit', 'input_modalities',
                     'supports_parallel_tool_calls', 'supports_reasoning_summaries'}
            if any(not isinstance(k, str) or k in owned for k in spec['metadata']):
                invalid()
            try:
                json.dumps(spec['metadata'], allow_nan=False)
            except (TypeError, ValueError):
                invalid()
        result[model] = spec
    return result


def valid_name(name):
    return isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name) is not None


@dataclass
class Endpoint:
    url: str = ""
    models: dict[str, dict] = field(default_factory=dict)
    default_model: str = ""
    env: dict[str, str] = field(default_factory=dict)


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
            if not isinstance(endpoint, dict) or set(endpoint) - {"url", "models", "default_model", "env"}:
                invalid(f"{path}.{cli}")
            url = endpoint.get("url", "")
            if not isinstance(url, str):
                invalid(f"{path}.{cli}.url")
            if url:
                try:
                    parsed = urlsplit(url)
                    # urlsplit separates components but does not validate URI syntax.
                    host = parsed.hostname or ""
                    valid = (parsed.scheme in {"http", "https"} and host
                             and parsed.username is None and parsed.password is None
                             and "?" not in url and "#" not in url and "\\" not in url
                             and "%" not in host
                             and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)
                             and not any(c in host for c in '<>"{}|^`'))
                    parsed.port
                except ValueError:
                    valid = False
                if not valid:
                    invalid(f"{path}.{cli}.url must be HTTP(S), without credentials, query or fragment")
            models = parse_models(endpoint.get('models', {}), cli, f'{path}.{cli}.models')
            default = endpoint.get('default_model', '')
            if not isinstance(default, str) or default and default not in models:
                invalid(f'{path}.{cli}.default_model must name a configured model')
            env = parse_env(endpoint.get('env', {}), f'{path}.{cli}.env')
            setattr(spec, cli, Endpoint(url, models, default, env))
        result[name] = spec
    return result


def validate_connections(config):
    errors = []
    for name, spec in config.external_providers.items():
        if spec.enabled and (not spec.api_key.strip() or not (spec.codex.url or spec.claude.url)):
            errors.append(f"external_providers.{name}: enabled provider requires an API key and a CLI URL")
    return errors
