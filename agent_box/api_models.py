"""Model lists external APIs publish; a listed model is a suggestion, not proof of access."""
import http.client
import json
from urllib.parse import urlencode, urlsplit
import urllib.error

from .external_config import EFFORTS, valid_model
from .usage import open_http

TIMEOUT = 5
# OpenRouter returns its whole catalog in one page today; the bound keeps a
# misbehaving cursor from holding the menu, and is reported, never hidden.
PAGES = 20
OPENAI = ('/models', lambda key: {'Authorization': 'Bearer ' + key})
ANTHROPIC = ('/v1/models', lambda key: {'x-api-key': key, 'anthropic-version': '2023-06-01'})


class ListError(Exception):
    """A short reason the list could not be read; never carries the key or a response body."""


class EndpointUnavailable(ListError):
    """The first request may establish that this endpoint does not implement model lists."""


def _fetch(url, headers):
    try:
        with open_http(url, headers={'User-Agent': 'agent-box', **headers}, timeout=TIMEOUT) as response:
            if response.status != 200 or 'Content-Range' in response.headers:
                raise ListError(f'incomplete model list response (HTTP {response.status})')
            return json.load(response)
    except urllib.error.HTTPError as exc:
        exc.close()
        error = EndpointUnavailable if exc.code in (404, 405, 501) else ListError
        raise error(f'HTTP {exc.code}') from None
    except (OSError, http.client.HTTPException):
        raise ListError('network error or timeout') from None
    except RecursionError:
        raise ListError('JSON response is too deeply nested') from None
    except ValueError:
        raise ListError('response is not JSON') from None


def _items(base, protocol, key, require_complete):
    """Every entry of the list at ``base``, paged by whichever cursor the response carries.

    OpenAI and OpenRouter page with a ``links.next`` URL, Anthropic with
    ``has_more``/``last_id``; the CLI a URL belongs to does not decide which
    one a provider answers with. A next URL on another scheme or host is not
    followed, as with a redirect, so the key goes only where it was configured
    to; a list cut short there, by a missing cursor or by the page bound is
    reported rather than shown as whole.
    """
    path, headers = protocol
    first = base.rstrip('/') + path
    origin = urlsplit(first)[:2]
    query = {'limit': 1000} if protocol is ANTHROPIC else {}
    page, items = first + ('?' + urlencode(query) if query else ''), []
    total = None
    cursor_paging = False
    for index in range(PAGES):
        try:
            data = _fetch(page, headers(key))
        except EndpointUnavailable as exc:
            if index:
                raise ListError(f'incomplete model list: {exc}') from None
            raise
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise ListError('not a model list')
        if (data.get('object') == 'error' or data.get('type') == 'error'
                or data.get('error') not in (None, False, '') or data.get('errors')
                or data.get('success') is False or data.get('ok') is False):
            raise ListError('provider reported an error instead of a model list')
        rows = data['data']
        items.extend(rows)
        links = data.get('links')
        following = links.get('next') if isinstance(links, dict) else None
        if require_complete:
            if (cursor_paging or 'first_id' in data or 'last_id' in data) and 'has_more' not in data:
                raise ListError('incomplete model list: cursor page omitted has_more')
            for cursor, position in (('first_id', 0), ('last_id', -1)):
                if cursor in data:
                    row = rows[position] if rows else None
                    boundary = row.get('id') if isinstance(row, dict) else None
                    if data[cursor] != boundary:
                        raise ListError(f'inconsistent model list: {cursor} does not match its page')
            if (links is not None and not isinstance(links, dict)
                    or following is not None and not (isinstance(following, str) and following.strip())
                    or 'has_more' in data and type(data['has_more']) is not bool):
                raise ListError('invalid model list pagination')
            if 'total_count' in data:
                count = data['total_count']
                if type(count) is not int or count < 0 or total is not None and count != total:
                    raise ListError('invalid or inconsistent model list total_count')
                total = count
            if ((index or 'links' in data) and total is None and 'has_more' not in data
                    and not (isinstance(links, dict) and 'next' in links)):
                raise ListError('incomplete model list: page omitted pagination status')
        if isinstance(following, str) and following:
            try:
                foreign = urlsplit(following)[:2] != origin
            except ValueError:
                foreign = True
            if foreign:
                raise ListError('the next page is on another scheme or host; the key is not sent there')
            page = following
        elif data.get('has_more') is True:
            if not isinstance(data.get('last_id'), str) or not data['last_id']:
                raise ListError('more pages announced without a cursor to reach them')
            page = first + '?' + urlencode({**query, 'after_id': data['last_id']})
            cursor_paging = True
        else:
            if total is not None and len(items) != total:
                raise ListError(f'incomplete model list: received {len(items)} of {total} entries')
            return items
    raise ListError(f'more than {PAGES} pages; showing none rather than a partial list')


def _number(*values):
    return next((v for v in values if type(v) is int and v > 0), None)


def _names(value):
    return isinstance(value, list) and all(isinstance(name, str) and name.strip() for name in value)


def routing_alias(model):
    """These published IDs select different models, not a fixed set of capabilities."""
    return model in ('openrouter/free', 'openrouter/auto')


def model_fields(item):
    """The shared model settings one list entry states; a field the API leaves out stays absent.

    Covers OpenAI (ID only), DeepSeek (``effort``), OpenRouter (``reasoning``,
    ``architecture``), OpenCode (``limit``, ``modalities``) and Anthropic. Values outside agent-box's
    vocabulary are dropped rather than refused: the list is advice. Silence
    about efforts leaves them alone; a stated empty set clears them.
    """
    fields = {}
    name = item.get('display_name') or item.get('name')
    if isinstance(name, str) and name.strip():
        fields['display_name'] = name.strip()
    if routing_alias(item.get('id')):
        return fields
    top = item.get('top_provider') if isinstance(item.get('top_provider'), dict) else {}
    limits = item.get('limit') if isinstance(item.get('limit'), dict) else {}
    context = _number(item.get('context_window'), item.get('context_length'),
                      limits.get('context'), item.get('max_input_tokens'), top.get('context_length'))
    if context:
        fields['context_window'] = context
    output = _number(item.get('max_output_tokens'), limits.get('output'), top.get('max_completion_tokens'),
                     item.get('max_tokens') if item.get('type') == 'model' else None)
    if output:
        fields['max_output_tokens'] = output
    capabilities = item.get('capabilities') if isinstance(item.get('capabilities'), dict) else {}
    architecture = item.get('architecture') if isinstance(item.get('architecture'), dict) else {}
    modalities = item.get('input_modalities')
    if modalities is None:
        modalities = architecture.get('input_modalities')
    if modalities is None and isinstance(item.get('modalities'), dict):
        modalities = item['modalities'].get('input')
        if isinstance(modalities, list):
            modalities = ['image' if isinstance(m, str) and m.startswith('image/') else m for m in modalities]
    image = capabilities.get('image_input')
    if isinstance(image, dict) and isinstance(image.get('supported'), bool):
        modalities = ['text', 'image'] if image['supported'] else ['text']
    if _names(modalities):
        fields['input_modalities'] = [m for m in ('text', 'image') if m in modalities]
    levels, default = None, None
    reasoning = capabilities.get('reasoning')
    if isinstance(reasoning, dict):
        if reasoning.get('supported') is False:
            levels = []
        else:
            if _names(reasoning.get('effort')):
                levels = reasoning['effort']
            preset = reasoning.get('default')
            if isinstance(preset, dict) and preset.get('type') == 'effort':
                default = preset.get('value')
    router = item.get('reasoning')
    gateway_reasoning = isinstance(router, dict) and isinstance(router.get('mandatory'), bool)
    if gateway_reasoning and 'supported_efforts' in router and router['supported_efforts'] is None:
        # OpenRouter defines explicit null as all gateway efforts; an omitted key makes no such promise.
        levels = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
        default = router.get('default_effort')
    for source, key, preferred in ((item.get('effort'), 'supported_levels', 'default_level'),
                                   (item.get('reasoning'), 'supported_efforts', 'default_effort')):
        if isinstance(source, dict):
            if _names(source.get(key)):
                levels = source[key]
            if preferred in source:
                default = source[preferred]
    if gateway_reasoning and router['mandatory']:
        if levels is not None:
            levels = [level for level in levels if level != 'none']
        if default == 'none':
            default = None
    effort = capabilities.get('effort')
    if isinstance(effort, dict) and isinstance(effort.get('supported'), bool):
        flags = {name: flag['supported'] for name, flag in effort.items()
                 if isinstance(flag, dict) and type(flag.get('supported')) is bool}
        if not effort['supported'] or flags:
            levels = [name for name in flags if effort['supported'] and flags[name]]
    if levels is not None:
        # A published empty subset means there is no selectable client effort.
        supported = [e for e in EFFORTS[1:] if e in levels]
        fields['supported_efforts'] = supported
        if default in supported:
            fields['effort'] = default
    elif default in EFFORTS[1:]:
        fields['effort'] = default
    return fields


def load(provider, *, require_complete=False):
    """``(models, error)`` for one provider dict from the config draft.

    The Codex URL is tried first: DeepSeek's Anthropic URL has no list, while
    an OpenAI-compatible one usually does. A provider without a key is not
    asked at all.
    """
    key = provider.get('api_key', '')
    if not key.strip():
        return {}, 'set the API key to load the model list'
    attempts = [(protocol, provider[cli]['url']) for protocol, cli in ((OPENAI, 'codex'), (ANTHROPIC, 'claude'))
                if provider.get(cli, {}).get('url')]
    if not attempts:
        return {}, 'set a Codex or Claude URL to load the model list'
    errors = []
    for protocol, url in attempts:
        try:
            # A draft URL is validated only on save, so a malformed one reaches here.
            items = _items(url, protocol, key, require_complete)
        except ValueError:
            errors.append(f'{url}: malformed URL')
            if require_complete:
                break
            continue
        except ListError as exc:
            errors.append(f'{url}: {exc}')
            if require_complete and not isinstance(exc, EndpointUnavailable):
                break
            continue
        models = {}
        for item in items:
            if require_complete and (not isinstance(item, dict) or not valid_model(item.get('id'))):
                return {}, 'cannot synchronize a list containing a missing or unsupported model ID'
            if require_complete and item['id'] in models:
                return {}, 'cannot synchronize a list containing a duplicate model ID'
            if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id'] not in models:
                models[item['id']] = model_fields(item)
        return models, None
    return {}, 'model list unavailable (' + '; '.join(errors) + ')'
