"""Model lists external APIs publish; a listed model is a suggestion, not proof of access."""
import http.client
from urllib.parse import urlencode, urlsplit
import urllib.error

from .external_config import EFFORTS
from .usage import http_json

TIMEOUT = 5
# OpenRouter returns its whole catalog in one page today; the bound keeps a
# misbehaving cursor from holding the menu, and is reported, never hidden.
PAGES = 20
OPENAI = ('/models', lambda key: {'Authorization': 'Bearer ' + key})
ANTHROPIC = ('/v1/models', lambda key: {'x-api-key': key, 'anthropic-version': '2023-06-01'})


class ListError(Exception):
    """A short reason the list could not be read; never carries the key or a response body."""


def _fetch(url, headers):
    try:
        return http_json(url, headers=headers, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ListError(f'HTTP {exc.code}') from None
    except (OSError, http.client.HTTPException):
        raise ListError('network error or timeout') from None
    except ValueError:
        raise ListError('response is not JSON') from None


def _items(base, protocol, key):
    """Every entry of the list at ``base``, paged by whichever cursor the response carries.

    OpenAI and OpenRouter page with a ``links.next`` URL, Anthropic with
    ``has_more``/``last_id``; the CLI a URL belongs to does not decide which
    one a provider answers with. A next URL on another host is not followed,
    as with a redirect, so the key goes only to the configured host; a list cut
    short there or by the page bound is reported rather than shown as whole.
    """
    path, headers = protocol
    first = base.rstrip('/') + path
    host = urlsplit(first).netloc
    query = {'limit': 1000} if protocol is ANTHROPIC else {}
    page, items = first + ('?' + urlencode(query) if query else ''), []
    for _ in range(PAGES):
        data = _fetch(page, headers(key))
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise ListError('not a model list')
        items += data['data']
        links = data.get('links') if isinstance(data.get('links'), dict) else {}
        following = links.get('next')
        if isinstance(following, str) and following:
            try:
                foreign = urlsplit(following).netloc != host
            except ValueError:
                foreign = True
            if foreign:
                raise ListError('the next page is on another host; the key is not sent there')
            page = following
        elif data.get('has_more') is True and isinstance(data.get('last_id'), str):
            page = first + '?' + urlencode({**query, 'after_id': data['last_id']})
        else:
            return items
    raise ListError(f'more than {PAGES} pages; showing none rather than a partial list')


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
    attempts = [(protocol, provider[cli]['url']) for protocol, cli in ((OPENAI, 'codex'), (ANTHROPIC, 'claude'))
                if provider.get(cli, {}).get('url')]
    if not attempts:
        return {}, 'set a Codex or Claude URL to load the model list'
    errors = []
    for protocol, url in attempts:
        try:
            # A draft URL is validated only on save, so a malformed one reaches here.
            items = _items(url, protocol, key)
        except ValueError:
            errors.append(f'{url}: malformed URL')
            continue
        except ListError as exc:
            errors.append(f'{url}: {exc}')
            continue
        models = {}
        for item in items:
            if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id'] not in models:
                models[item['id']] = model_fields(item)
        return models, None
    return {}, 'model list unavailable (' + '; '.join(errors) + ')'
