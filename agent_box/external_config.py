"""Named API connections; CLI identity and credentials have different owners."""
import copy
from dataclasses import asdict, dataclass, field
import json
import re
from urllib.parse import urlsplit

from .errors import AgentBoxError

EFFORTS = ('default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
API_EFFORTS = {'codex': EFFORTS, 'claude': ('default', 'low', 'medium', 'high', 'xhigh', 'max')}
CODEX_OWNED_CONFIG = {'model', 'model_provider', 'model_providers', 'model_catalog_json',
                      'forced_login_method', 'cli_auth_credentials_store', 'chatgpt_base_url',
                      'profiles', 'profile'}


def toml_string(value: str) -> str:
    r"""A TOML basic string for text agent-box writes into a ``-c`` value.

    Codex parses the value portion of ``-c`` as TOML, and ``json.dumps`` is not
    quite that escaper: it writes a non-BMP character as a UTF-16 surrogate
    pair -- two escapes for one emoji -- and TOML rejects a surrogate escape
    because a surrogate is not a Unicode scalar value. It also leaves DEL
    literal when told not to escape non-ASCII, which a basic string forbids.
    Control characters are escaped here and everything else is literal, which
    TOML allows.
    """
    escaped = []
    for char in value:
        if char in '\\"':
            escaped.append("\\" + char)
        elif char == "\n":
            escaped.append("\\n")
        elif char == "\r":
            escaped.append("\\r")
        elif char == "\t":
            escaped.append("\\t")
        elif char < " " or char == "\x7f":
            escaped.append(f"\\u{ord(char):04X}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def has_toml_spelling(value: str) -> bool:
    """Whether TOML can carry this text at all.

    Only a JSON source can put a lone surrogate in a name or an option -- a
    TOML document cannot hold one -- and no TOML string can spell it: writing
    it would put invalid UTF-8 in the argv Codex parses.
    """
    return not any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def unspelled_path(value, where):
    """Where the first text UTF-8 cannot carry sits, or None.

    Only a hand-written JSON ``\\uXXXX`` escape produces a lone surrogate, and
    it reaches argv, the environment and TOML alike with no representation in
    any of them. The offending key is reported through ``ascii``: text no
    encoding can carry is text no terminal can print either.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and not has_toml_spelling(key):
                return f"{where}.{ascii(key)}"
            found = unspelled_path(item, f"{where}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = unspelled_path(item, f"{where}[{index}]")
            if found:
                return found
    elif isinstance(value, str) and not has_toml_spelling(value):
        return where
    return None


def codex_options(raw, path, *, provider=False):
    """Codex validates native option semantics; agent-box retains API/auth ownership."""
    reserved = ({'base_url', 'env_key', 'experimental_bearer_token', 'requires_openai_auth', 'auth'}
                if provider else CODEX_OWNED_CONFIG)
    def valid(value):
        if isinstance(value, dict):
            return all(isinstance(k, str) and k and valid(v) for k, v in value.items())
        if isinstance(value, list):
            return all(valid(v) for v in value)
        return type(value) in (str, bool, int, float)
    if (not isinstance(raw, dict) or not valid(raw) or any(k in reserved for k in raw)
            or not provider and (any('remote' in k for k in raw)
                or isinstance(raw.get('features'), dict) and any('remote' in k for k in raw['features']))):
        raise AgentBoxError(f'Invalid {path}: expected native options without launcher-owned API/auth/Remote Control fields')
    bad_path = unspelled_path(raw, path)
    if bad_path:
        raise AgentBoxError(f'Invalid {bad_path}: TOML cannot carry these characters, '
                            'and Codex parses -c values as TOML')
    try:
        json.dumps(raw, allow_nan=False)
    except (TypeError, ValueError):
        raise AgentBoxError(f'Invalid {path}: expected finite JSON values') from None
    if provider:
        for field in ('http_headers', 'env_http_headers'):
            headers = raw.get(field, {})
            if (not isinstance(headers, dict) or any(not isinstance(v, str) or
                    k.lower() in {'authorization', 'proxy-authorization', 'x-api-key'} for k, v in headers.items())):
                raise AgentBoxError(f'Invalid {path}.{field}: expected string headers without authentication overrides')
    return dict(raw)


def valid_model(model, cli=None):
    """A configured ID never carries ``[1m]``; Claude accepts it on an explicit launch ID."""
    suffix = r'(?:\[1m\])?' if cli == 'claude' else ''
    return isinstance(model, str) and re.fullmatch(r'~?[A-Za-z0-9][A-Za-z0-9._:/-]*' + suffix, model) is not None


CLI_MODEL_DEFAULTS = {
    'codex': dict(auto_compact_token_limit=0, env={}, supports_parallel_tool_calls=False,
                  supports_reasoning_summaries=False, metadata={}),
    'claude': dict(auto_compact_token_limit=0, env={}),
}
SHARED_DEFAULTS = dict(effort='default', supported_efforts=[], context_window=0,
                       max_output_tokens=0, input_modalities=['text'])


def model_defaults():
    """One model for both CLIs: what the API describes is shared, CLI tuning has its own block."""
    return copy.deepcopy(dict(display_name='', **SHARED_DEFAULTS, **CLI_MODEL_DEFAULTS))


def external_model_entry():
    """Null fields follow discovery or the connection; even text-only can be an explicit override."""
    return dict.fromkeys(('display_name', *SHARED_DEFAULTS)) | copy.deepcopy(CLI_MODEL_DEFAULTS) | {'discovered': {}}


def effective_model(entry, defaults):
    manual = {k: v for k, v in entry.items() if v is not None and k != 'discovered'}
    reported = entry.get('discovered', {})
    settings = {**model_defaults(), **defaults, **reported, **manual}
    effort, levels = settings['effort'], settings['supported_efforts']
    if reported.get('supported_efforts') == [] and 'supported_efforts' not in manual:
        if manual.get('effort', 'default') == 'default':
            settings.update(effort='default', effort_unavailable=True)
        else:
            settings['supported_efforts'] = [effort]
    if levels and effort != 'default' and effort not in levels:
        # Compare the final fields' sources. An intermediate list may have been overridden again.
        for layer in (manual, reported, defaults):
            if 'effort' in layer:
                if 'supported_efforts' not in layer:
                    settings['supported_efforts'] = [e for e in EFFORTS[1:] if e in levels or e == effort]
                break
            if 'supported_efforts' in layer:
                settings['effort'] = 'default'
                break
    return settings


def cli_settings(spec, cli):
    """The flat settings one CLI launches with, derived from a shared model entry.

    Claude cannot request an effort outside its own levels, so a shared
    default it lacks becomes the CLI default instead of a launch error. The
    declared list stays whole: narrowed to Claude's levels it could become
    empty, which means "unknown" and would lift the model's restriction.
    """
    flat = {key: spec[key] for key in ('display_name', 'effort', 'supported_efforts', 'context_window',
                                      'max_output_tokens')}
    flat.update(spec[cli])
    if spec.get('effort_unavailable'):
        flat['effort_unavailable'] = True
    if cli == 'codex':
        flat['input_modalities'] = spec['input_modalities']
    else:
        flat['effort'] = spec['effort'] if spec['effort'] in API_EFFORTS['claude'] else 'default'
    return flat


def cli_models(models, cli, defaults):
    return {model: cli_settings(effective_model(spec, defaults), cli) for model, spec in models.items()}


def parse_env(value, path, cli):
    # API identity and integration paths belong to the launcher, not model tuning.
    reserved = {'HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'CLAUDECODE',
                'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_ORG_ID', 'OPENAI_ORGANIZATION',
                'OPENAI_PROJECT_ID', 'CODEX_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
                'ANTHROPIC_BASE_URL', 'ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_MODEL',
                'ANTHROPIC_CUSTOM_HEADERS'}
    if not isinstance(value, dict):
        raise AgentBoxError(f'Invalid external provider configuration: {path} must be an environment object')
    for key, item in value.items():
        if (not isinstance(key, str) or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) is None
                or not isinstance(item, str) or '\0' in item or key in reserved
                or key.startswith(('AGENT_BOX_', 'ANTHROPIC_DEFAULT_', 'CLAUDE_CODE_OAUTH_',
                                   'CLAUDE_CODE_USE_', 'CLAUDE_CODE_API_KEY_'))
                or 'REMOTE_CONTROL' in key):
            raise AgentBoxError(f'Invalid external provider configuration: {path}; invalid or launcher-owned variable')
        if cli == 'claude' and key == 'CLAUDE_CODE_EFFORT_LEVEL' and item not in API_EFFORTS['claude'][1:]:
            raise AgentBoxError(f'Invalid external provider configuration: {path}; invalid Claude effort level')
    return dict(value)


def model_settings(models, model, fallback):
    """An unconfigured ID can use connection defaults without inheriting a sibling's settings."""
    return models.get(model, models.get(model.removesuffix('[1m]'), fallback))


def model_effort_error(endpoint, model, cli, effort):
    spec = model_settings(endpoint.models, model, endpoint.fallback)
    if effort in (None, 'default'):
        effort = spec['effort']
        if effort == 'default' and cli == 'claude':
            effort = {**endpoint.env, **spec['env']}.get('CLAUDE_CODE_EFFORT_LEVEL', 'default')
        elif effort == 'default' and cli == 'codex':
            effort = endpoint.config.get('model_reasoning_effort', 'default')
    if ((spec['supported_efforts'] or spec.get('effort_unavailable'))
            and effort not in (None, 'default') and effort not in spec['supported_efforts']):
        return f'Requested effort is not supported by configured {cli} model {model}; choose default or a supported level'
    return None


def validate_shared(values, path):
    """Check a single source even if another layer currently masks its values."""
    schema = {'display_name': '', **SHARED_DEFAULTS}
    if not isinstance(values, dict) or any(k not in schema or type(v) is not type(schema[k])
                                           for k, v in values.items()):
        raise AgentBoxError(f'Invalid {path}: invalid model settings')
    efforts = values.get('supported_efforts', [])
    effort = values.get('effort', 'default')
    modalities = values.get('input_modalities', ['text'])
    if (effort not in EFFORTS or any(e not in EFFORTS[1:] for e in efforts) or len(set(efforts)) != len(efforts)
            or efforts and effort != 'default' and effort not in efforts
            or any(m not in ('text', 'image') for m in modalities)
            or len(set(modalities)) != len(modalities)
            or values.get('context_window', 0) < 0 or values.get('max_output_tokens', 0) < 0):
        raise AgentBoxError(f'Invalid {path}: invalid model settings')


def parse_models(raw, path, connection_defaults):
    if not isinstance(raw, dict):
        raise AgentBoxError(f'Invalid external provider configuration: {path} must map model IDs to settings')
    result = {}
    for model, values in raw.items():
        def invalid():
            raise AgentBoxError(f'Invalid external provider configuration: {path}.{model}; invalid model settings')
        defaults = external_model_entry()
        if not valid_model(model) or not isinstance(values, dict) or set(values) - set(defaults):
            invalid()
        spec = {**defaults, **values}
        fields = ('display_name', *SHARED_DEFAULTS)
        validate_shared({k: spec[k] for k in fields if spec[k] is not None}, f'{path}.{model}')
        validate_shared(spec['discovered'], f'{path}.{model}.discovered')
        effective = effective_model(spec, connection_defaults)
        validate_shared({k: effective[k] for k in fields}, f'{path}.{model}')
        for cli, block in CLI_MODEL_DEFAULTS.items():
            if not isinstance(spec[cli], dict) or set(spec[cli]) - set(block):
                invalid()
            spec[cli] = {**defaults[cli], **spec[cli]}
            if any(type(spec[cli][key]) is not type(default) for key, default in block.items()):
                invalid()
        for cli in CLI_MODEL_DEFAULTS:
            context, compact = effective['context_window'], spec[cli]['auto_compact_token_limit']
            if compact < 0 or context and compact >= context:
                invalid()
            spec[cli]['env'] = parse_env(spec[cli]['env'], f'{path}.{model}.{cli}.env', cli)
        # Typed fields have one owner; advanced native metadata fills other slots.
        owned = {'slug', 'display_name', 'default_reasoning_level', 'supported_reasoning_levels',
                 'context_window', 'auto_compact_token_limit', 'input_modalities',
                 'supports_parallel_tool_calls', 'supports_reasoning_summaries'}
        if any(not isinstance(k, str) or k in owned for k in spec['codex']['metadata']):
            invalid()
        try:
            json.dumps(spec['codex']['metadata'], allow_nan=False)
        except (TypeError, ValueError):
            invalid()
        result[model] = spec
    return result


def valid_name(name):
    return isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name) is not None


@dataclass
class Endpoint:
    """``models`` is this CLI's flat view of the provider's list, derived on parse and never saved.

    Launch code reads one CLI's settings, and a route fingerprint covers what
    that CLI would run; the shared list stays the single stored copy.
    """
    url: str = ""
    models: dict[str, dict] = field(default_factory=dict)
    default_model: str = ""
    env: dict[str, str] = field(default_factory=dict)
    fallback: dict = field(default_factory=dict, kw_only=True)


@dataclass
class CodexEndpoint(Endpoint):
    provider_options: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)


@dataclass
class ExternalProvider:
    enabled: bool = False
    api_key: str = ""
    models: dict[str, dict] = field(default_factory=dict)
    codex: CodexEndpoint = field(default_factory=CodexEndpoint)
    claude: Endpoint = field(default_factory=Endpoint)
    model_defaults: dict = field(default_factory=lambda: copy.deepcopy(SHARED_DEFAULTS))

    def to_dict(self):
        data = asdict(self)
        for cli in CLI_MODEL_DEFAULTS:
            del data[cli]['models']
            del data[cli]['fallback']
        return data


def parse_connections(data):
    if data is None:
        return {}
    def invalid(path):
        raise AgentBoxError(f"Invalid external provider configuration: {path}")
    if not isinstance(data, dict):
        invalid("agents.external_apis must be an object")
    result = {}
    for name, raw in data.items():
        if not valid_name(name):
            invalid("provider name must match [a-z][a-z0-9_-]{0,47}")
        path = f"agents.external_apis.{name}"
        if not isinstance(raw, dict) or set(raw) - {"enabled", "api_key", "models", "codex", "claude", "model_defaults"}:
            invalid(path)
        unspelled = unspelled_path(raw, path)
        if unspelled:
            invalid(f"{unspelled}: a lone surrogate has no UTF-8 form")
        spec = ExternalProvider()
        for key, kind in (("enabled", bool), ("api_key", str)):
            value = raw.get(key, getattr(spec, key))
            if type(value) is not kind:
                invalid(f"{path}.{key}")
            setattr(spec, key, value)
        fallback = raw.get('model_defaults', {})
        validate_shared(fallback, f'{path}.model_defaults')
        if 'display_name' in fallback:
            invalid(f'{path}.model_defaults.display_name')
        spec.model_defaults.update(fallback)
        spec.models = parse_models(raw.get('models', {}), f'{path}.models', spec.model_defaults)
        for cli in ("codex", "claude"):
            endpoint = raw.get(cli, {})
            allowed = {'url', 'default_model', 'env'} | ({'provider_options', 'config'} if cli == 'codex' else set())
            if not isinstance(endpoint, dict) or set(endpoint) - allowed:
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
            models = cli_models(spec.models, cli, spec.model_defaults)
            default = endpoint.get('default_model', '')
            if not isinstance(default, str) or default and default not in models:
                invalid(f'{path}.{cli}.default_model must name a configured model')
            env = parse_env(endpoint.get('env', {}), f'{path}.{cli}.env', cli)
            parsed_endpoint = (CodexEndpoint(url, models, default, env,
                               codex_options(endpoint.get('provider_options', {}), f'{path}.codex.provider_options', provider=True),
                               codex_options(endpoint.get('config', {}), f'{path}.codex.config'))
                               if cli == 'codex' else Endpoint(url, models, default, env))
            parsed_endpoint.fallback = cli_settings(effective_model({}, spec.model_defaults), cli)
            for model in models:
                if model_effort_error(parsed_endpoint, model, cli, 'default'):
                    invalid(f'{path}.{cli}.models: default effort is not supported')
            setattr(spec, cli, parsed_endpoint)
        result[name] = spec
    return result


def validate_connections(config):
    errors = []
    for name, spec in config.agents.external_apis.items():
        if spec.enabled and (not spec.api_key.strip() or not (spec.codex.url or spec.claude.url)):
            errors.append(f"agents.external_apis.{name}: enabled provider requires an API key and a CLI URL")
    for path, candidate in config.workflow.candidates():
        api, cli = candidate.get('api_provider'), candidate.get('provider')
        # A malformed name is WorkflowConfig.validate's error to report, not a lookup to crash on.
        connection = config.agents.external_apis.get(api) if isinstance(api, str) else None
        if connection and cli in ('codex', 'claude') and candidate.get('model'):
            error = model_effort_error(getattr(connection, cli), candidate['model'], cli, candidate['effort'])
            if error:
                errors.append(f'{path}: {error}')
    return errors


def model_warnings(config):
    """Unknown explicit IDs remain usable, but inherit no catalog metadata."""
    warnings = []
    for path, candidate in config.workflow.candidates():
        api, model = candidate.get('api_provider'), candidate.get('model', '')
        connection = config.agents.external_apis.get(api) if isinstance(api, str) else None
        if connection and connection.enabled and model and model.removesuffix('[1m]') not in connection.models:
            warnings.append(f'{path}: {candidate["provider"]}@{api}:{model} has no configured model settings; '
                            'shared settings follow the connection defaults; CLI-specific settings use generic defaults. '
                            f'Match the model ID or add it under agents.external_apis.{api}.models.')
    return warnings
