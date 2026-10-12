"""Model lists external APIs publish; a listed model is a suggestion, not proof of access."""
import http.client
from urllib.parse import urlencode, urlsplit
import urllib.error

from .external_config import EFFORTS
from .usage import http_json

TIMEOUT = 5
# OpenRouter returns its whole catalog in one 500-model page today; the bound
# only keeps a misbehaving cursor from holding the menu.
PAGES = 20


class ListError(Exception):
    """A short reason the list could not be read; never carries the key or a response body."""


def _fetch(url, headers):
    try:
        return http_json(url, headers=headers, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise ListError(f'HTTP {exc.code}') from None
    except (OSError, http.client.HTTPException):
        raise ListError('network error or timeout') from None
    except ValueError:
        raise ListError('response is not JSON') from None


def _openai(url, key):
    """``GET {url}/models``: OpenAI's shape, with OpenRouter's ``links.next`` pages."""
    items, page = [], url.rstrip('/') + '/models'
    host = urlsplit(page).netloc
    for _ in range(PAGES):
        data = _fetch(page, {'Authorization': 'Bearer ' + key})
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise ListError('not an OpenAI-compatible model list')
        items += data['data']
        following = (data.get('links') or {}).get('next') if isinstance(data.get('links'), dict) else None
        # The key goes only to the configured host, as with a redirect.
        if not isinstance(following, str) or urlsplit(following).netloc != host:
            return items
        page = following
    return items


def _anthropic(url, key):
    """``GET {url}/v1/models``: Anthropic's shape, paged by ``after_id``."""
    items, after = [], None
    for _ in range(PAGES):
        query = {'limit': 1000, **({'after_id': after} if after else {})}
        data = _fetch(url.rstrip('/') + '/v1/models?' + urlencode(query),
                      {'x-api-key': key, 'anthropic-version': '2023-06-01'})
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise ListError('not an Anthropic-compatible model list')
        items += data['data']
        after = data.get('last_id')
        if not data.get('has_more') or not isinstance(after, str):
            return items
    return items


def _number(*values):
    return next((v for v in values if type(v) is int and v > 0), None)


def model_fields(item):
    """The shared model settings one list entry states; a field the API leaves out stays absent.

    Covers OpenAI (ID only), DeepSeek (``effort``), OpenRouter (``reasoning``,
    ``architecture``) and Anthropic (``capabilities``). Values outside agent-box's
    vocabulary are dropped rather than refused: the list is advice.
    """
    fields = {}
    name = item.get('display_name') or item.get('name')
    if isinstance(name, str) and name.strip():
        fields['display_name'] = name.strip()
    top = item.get('top_provider') if isinstance(item.get('top_provider'), dict) else {}
    context = _number(item.get('context_window'), item.get('context_length'),
                      item.get('max_input_tokens'), top.get('context_length'))
    if context:
        fields['context_window'] = context
    capabilities = item.get('capabilities') if isinstance(item.get('capabilities'), dict) else {}
    architecture = item.get('architecture') if isinstance(item.get('architecture'), dict) else {}
    modalities = item.get('input_modalities') or architecture.get('input_modalities')
    image = capabilities.get('image_input')
    if isinstance(image, dict) and isinstance(image.get('supported'), bool):
        modalities = ['text', 'image'] if image['supported'] else ['text']
    if isinstance(modalities, list):
        known = [m for m in ('text', 'image') if m in modalities]
        if known:
            fields['input_modalities'] = known
    levels, default = None, None
    for source, key, preferred in ((item.get('effort'), 'supported_levels', 'default_level'),
                                   (item.get('reasoning'), 'supported_efforts', 'default_effort')):
        if isinstance(source, dict) and isinstance(source.get(key), list):
            levels, default = source[key], source.get(preferred)
    effort = capabilities.get('effort')
    if isinstance(effort, dict) and effort.get('supported') is True:
        levels = [name for name, support in effort.items() if isinstance(support, dict) and support.get('supported') is True]
    if levels is not None:
        supported = [e for e in EFFORTS[1:] if e in levels]
        if supported:
            fields['supported_efforts'] = supported
            fields['effort'] = default if default in supported else 'default'
    return fields


def load(provider):
    """``(models, error)`` for one provider dict from the config draft.

    The Codex URL is tried first: DeepSeek's Anthropic URL has no list, while
    an OpenAI-compatible one usually does. A provider without a key is not
    asked at all.
    """
    key = provider.get('api_key', '')
    if not key.strip():
        return {}, 'set the API key to load the model list'
    attempts = [(fetch, provider[cli]['url']) for fetch, cli in ((_openai, 'codex'), (_anthropic, 'claude'))
                if provider.get(cli, {}).get('url')]
    if not attempts:
        return {}, 'set a Codex or Claude URL to load the model list'
    errors = []
    for fetch, url in attempts:
        try:
            items = fetch(url, key)
        except ListError as exc:
            errors.append(f'{url}: {exc}')
            continue
        models = {}
        for item in items:
            if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id'] not in models:
                models[item['id']] = model_fields(item)
        return models, None
    return {}, 'model list unavailable (' + '; '.join(errors) + ')'
