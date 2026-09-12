"""Per-process API selection; user configuration and RC supervisors keep their owners."""
import json
import hashlib
from dataclasses import asdict
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import tomllib

from .atomicio import atomic_write_json
from .errors import AgentBoxError
from .external_config import valid_name
from .paths import SESSION_TEMP_ROOT

API_ENV = 'AGENT_BOX_EXTERNAL_API_KEY'
BASELINE_ENV = 'AGENT_BOX_API_BASELINE'
PROVIDER_ENV = 'AGENT_BOX_API_PROVIDER'
_AUTH_NAMES = {
    'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_ORG_ID', 'OPENAI_ORGANIZATION',
    'OPENAI_PROJECT_ID', 'CODEX_API_KEY', API_ENV,
    'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR',
    'CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR', 'CLAUDE_CODE_USE_BEDROCK',
    'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CODE_EFFORT_LEVEL',
    'CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'CLAUDECODE',
}


def auth_name(name):
    return name in _AUTH_NAMES or name.startswith('ANTHROPIC_')


def builtin_environment(environment):
    """Restore the environment before ext, including opt-in built-in API keys."""
    env = dict(environment)
    baseline = env.pop(BASELINE_ENV, None)
    env.pop(PROVIDER_ENV, None)
    if baseline:
        try:
            original = json.loads(Path(baseline).read_text())
            if (not isinstance(original, dict) or
                    any(not auth_name(k) or not isinstance(v, str) for k, v in original.items())):
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise AgentBoxError('Cannot restore the parent API environment; restart through agent-box ext') from None
        env = {k: v for k, v in env.items() if not auth_name(k)}
        env.update(original)
    return env


def connection(config, name, cli):
    if not valid_name(name) or cli not in {'codex', 'claude'}:
        raise AgentBoxError('External launch requires a provider name and codex or claude')
    spec = config.external_providers.get(name)
    if spec is None or not spec.enabled:
        raise AgentBoxError(f'External provider {name} is missing or disabled; configure External API providers')
    endpoint = getattr(spec, cli)
    if not spec.api_key.strip() or not endpoint.url:
        raise AgentBoxError(f'External provider {name} requires an API key and a {cli} URL')
    return spec, endpoint


def route_identity(config, name, cli):
    _, endpoint = connection(config, name, cli)
    # Credentials stay private; rotation does not change the routed endpoint.
    payload = json.dumps([name, cli, asdict(endpoint)], sort_keys=True).encode()
    return {'api_provider': name, 'api_fingerprint': hashlib.sha256(payload).hexdigest()}


def check_route(config, route):
    identity = route_identity(config, route['api_provider'], route['provider'])
    if route.get('api_fingerprint') and route['api_fingerprint'] != identity['api_fingerprint']:
        raise AgentBoxError('External API definition changed after prepare; prepare a new route before execution')


def invocation_model(cli, args):
    """Reject endpoint/RC/session-management overrides before any launch side effects."""
    model = None
    commands = ({'agents', 'login', 'logout', 'mcp', 'plugin',
                'app-server', 'remote-control', 'completion', 'update', 'doctor', 'sandbox',
                'debug', 'apply', 'a', 'resume', 'fork', 'queue', 'archive', 'delete',
                'migrate-rollouts', 'unarchive', 'cloud', 'exec-server', 'features', 'help'}
                if cli == 'codex' else {'agents', 'auth', 'mcp', 'plugin', 'doctor',
                'attach', 'logs', 'stop', 'rm', 'import', 'install', 'auto-mode', 'gateway',
                'plugins', 'project', 'respawn', 'setup-token', 'kill', 'ultrareview', 'upgrade', 'update'})
    blocked = ({'--remote', '--remote-auth-token-env', '--oss', '--local-provider', '--profile', '-p'}
               if cli == 'codex' else
               {'--remote-control', '--rc', '--cloud', '--teleport', '--settings', '--setting-sources',
                '--resume', '-r', '--continue', '-c', '--fork-session', '--from-pr', '--session-id',
                '--environment', '--tmux', '--background', '--bg'})
    values = ({'--model', '-m', '--config', '-c', '--image', '-i', '--cd', '-C', '--sandbox', '-s',
              '--output-schema', '--output-last-message', '-o', '--base', '--commit', '--title',
              '--ask-for-approval', '-a', '--add-dir', '--enable', '--disable', '--effort',
              } if cli == 'codex' else {'--model', '--effort', '--name', '-n', '--add-dir',
              '--mcp-config', '--agent', '--agents', '--permission-mode', '--tools', '--allowedTools',
              '--allowed-tools', '--disallowedTools', '--disallowed-tools', '--append-system-prompt',
              '--system-prompt', '--output-format', '--input-format', '--json-schema', '--max-turns'})
    index, subcommand, print_mode, positionals = 0, None, False, []
    while index < len(args):
        arg = args[index]
        if arg == '--':
            break
        key, equal, value = arg.partition('=')
        if cli == 'claude' and arg.startswith('-') and not arg.startswith('--'):
            for flag in arg[1:]:
                if flag in 'rc':
                    raise AgentBoxError('ext cannot resume another session')
                if flag == 'p':
                    print_mode = True
                # These consume the rest as a value, not as more short flags.
                if flag in 'dnw' or flag not in 'phv':
                    break
        compact = ('-p',) if cli == 'codex' else ('-r', '-c')
        if (key in blocked or (arg.startswith(compact) and not arg.startswith('--'))
                or key.startswith('--remote-control')):
            raise AgentBoxError('ext cannot override API/profile/Remote Control or resume another session')
        if cli == 'codex' and arg.startswith('-c') and arg != '-c' and not arg.startswith('--'):
            key, equal, value = '-c', '=', arg[2:]
        if cli == 'codex' and arg.startswith('-m') and arg != '-m' and not arg.startswith('--'):
            key, equal, value = '-m', '=', arg[2:]
        if key in values and not equal:
            index += 1
            if index == len(args):
                raise AgentBoxError('External CLI option needs a value')
            value = args[index]
        if key in {'--model', '-m'}:
            model = value
        if cli == 'claude' and key in {'-p', '--print'}:
            print_mode = True
        if cli == 'codex' and key in {'-c', '--config'}:
            field = value.partition('=')[0].strip()
            try:
                root = next(iter(tomllib.loads(field + '=0')))
            except (ValueError, StopIteration):
                raise AgentBoxError('Invalid external CLI configuration override') from None
            if (root in {'model', 'model_provider', 'model_providers', 'model_catalog_json',
                                       'forced_login_method', 'chatgpt_base_url', 'profiles', 'profile'}
                    or 'remote' in field or root == 'features'):
                raise AgentBoxError('ext owns API/model catalog and Remote Control settings; use --model for the model')
        if key in {'--enable', '--disable'} and 'remote' in value:
            raise AgentBoxError('ext cannot override Remote Control features')
        if not arg.startswith('-'):
            if cli == 'codex' and subcommand is None and not positionals and arg in {'exec', 'e', 'review'}:
                subcommand = arg
                commands = {'resume'} if arg in {'exec', 'e'} else set()
            elif cli == 'codex' and subcommand in {'exec', 'e'} and not positionals and arg == 'review':
                subcommand, commands = 'review', set()
            else:
                # Check every unconsumed positional: an unknown value-taking option
                # must not hide a following management command.
                positionals.append(arg)
                if cli == 'codex' and arg in commands:
                    raise AgentBoxError('ext cannot run service, authentication or resume commands')
        index += 1
    if cli == 'claude' and not print_mode and any(arg in commands for arg in positionals):
        raise AgentBoxError('ext cannot run service, authentication or resume commands')
    return model


def selected_model(config, name, cli, args):
    _, endpoint = connection(config, name, cli)
    model = invocation_model(cli, args) or next(iter(endpoint.models), None)
    if not model or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*', model) is None:
        raise AgentBoxError('External launch requires a model ID: configure models or pass --model ID')
    return model


def catalog(models):
    # Conservative generic capabilities; endpoint-specific context limits remain unknown.
    return {'models': [{
        'slug': model, 'display_name': model, 'description': 'Configured external model',
        'default_reasoning_level': None, 'supported_reasoning_levels': [],
        'shell_type': 'shell_command', 'visibility': 'list', 'supported_in_api': True, 'priority': i,
        'base_instructions': 'Follow the user request and repository instructions. Use tools when needed and verify changes.',
        'supports_reasoning_summaries': False, 'support_verbosity': False, 'default_verbosity': None,
        'apply_patch_tool_type': 'freeform', 'web_search_tool_type': 'text',
        'truncation_policy': {'mode': 'tokens', 'limit': 10000},
        'supports_parallel_tool_calls': False, 'context_window': None, 'auto_compact_token_limit': None,
        'input_modalities': ['text'], 'experimental_supported_tools': [], 'supports_search_tool': False,
    } for i, model in enumerate(models)]}


def prepare(command, name, cli, config, directory, environment, *, model=None):
    """Shared by the interactive wrapper and managed exec/-p; secrets stay in env/files."""
    spec, endpoint = connection(config, name, cli)
    model = model or selected_model(config, name, cli, command[1:])
    original = builtin_environment(environment)
    directory = Path(directory)
    baseline = directory / 'api-baseline.json'
    atomic_write_json(baseline, {k: v for k, v in original.items() if auth_name(k)}, 0o600)
    env = {k: v for k, v in original.items() if not auth_name(k)}
    env.update({PROVIDER_ENV: name, BASELINE_ENV: str(baseline)})
    models = list(dict.fromkeys([*endpoint.models, model]))
    args = list(command[1:])
    boundary = args.index('--') if '--' in args else len(args)
    before, tail = args[:boundary], args[boundary:]
    if cli == 'codex':
        env[API_ENV] = spec.api_key
        path = directory / 'models.json'
        atomic_write_json(path, catalog(models), 0o600)
        table = ('name="agent-box external", base_url=' + json.dumps(endpoint.url) +
                 ', env_key=' + json.dumps(API_ENV) + ', wire_api="responses", '
                 'requires_openai_auth=false, supports_websockets=false')
        overrides = ['model=' + json.dumps(model), 'model_provider="agent_box_external"',
                     'model_providers.agent_box_external={' + table + '}',
                     'forced_login_method="api"', 'model_catalog_json=' + json.dumps(str(path)),
                     'web_search="disabled"']
        # Global -c options precede a possible managed exec subcommand.
        return [command[0], *[arg for value in overrides for arg in ('-c', value)], *before, *tail], env
    clean = {k: '' for k in _AUTH_NAMES | {k for k in original if k.startswith('ANTHROPIC_')}}
    clean.update({'ANTHROPIC_API_KEY': spec.api_key, 'ANTHROPIC_BASE_URL': endpoint.url,
                  'ANTHROPIC_MODEL': model, 'ANTHROPIC_DEFAULT_MODEL': model})
    for family in ('OPUS', 'SONNET', 'HAIKU', 'FABLE'):
        clean['ANTHROPIC_DEFAULT_' + family + '_MODEL'] = model
    env.update(clean)
    settings = {'env': clean, 'apiKeyHelper': '', 'model': model,
                'disableRemoteControl': True, 'remoteControlAtStartup': False,
                'modelPicker': {'replaceBuiltInOptions': True, 'options': [{'model': m} for m in models]}}
    # Managed fast settings use the same single --settings argument.
    if '--settings' in before:
        i = before.index('--settings')
        settings = {**json.loads(before[i + 1]), **settings}
        del before[i:i + 2]
    path = directory / 'claude-settings.json'
    atomic_write_json(path, settings, 0o600)
    return [command[0], *before, '--settings', str(path),
            *([] if model_option(before, cli) else ['--model', model]), *tail], env


def model_option(args, cli):
    return any(a == '--model' or a.startswith('--model=') or
               (cli == 'codex' and a.startswith('-m') and not a.startswith('--')) for a in args)


def main(args):
    if len(args) < 3:
        raise AgentBoxError('Invalid internal external launch')
    name, cli, *command = args
    from .config import effective_config
    config = effective_config(include_project=False)
    with tempfile.TemporaryDirectory(prefix='ab-api-', dir=SESSION_TEMP_ROOT) as temp:
        command, env = prepare(command, name, cli, config, temp, os.environ)
        process = subprocess.Popen(command, env=env)
        previous = {}
        def forward(signum, frame):
            process.send_signal(signum)
        try:
            for sig in (signal.SIGTERM, signal.SIGHUP):
                previous[sig] = signal.signal(sig, forward)
            return process.wait()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
