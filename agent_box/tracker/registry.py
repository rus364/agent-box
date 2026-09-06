"""Explicit capabilities keep unimplemented products from looking usable."""
import os
from pathlib import Path

from ..atomicio import read_json
from ..config import TRACKER_PROVIDERS, effective_config
from ..repo_binding import server_identity
from .core import TrackerError


def make_adapter(binding=None, provider=None, config=None):
    config = config or effective_config()
    provider = provider or (binding.provider if binding else config.tracker.provider)
    if provider not in TRACKER_PROVIDERS:
        raise TrackerError(f"Unknown tracker provider {provider}; choose {', '.join(TRACKER_PROVIDERS)}")
    if provider != "vikunja":
        raise TrackerError(f"Tracker provider '{provider}' is not implemented; configure Vikunja with agent-box config", "unsupported")
    if not config.tracker.enabled:
        raise TrackerError("Tracker is disabled; enable it with agent-box config", "disabled")
    connection = config.tracker.connection(provider)
    if not connection.usable:
        raise TrackerError(f"Configure the {provider} server and token with agent-box config", "configuration")
    url = server_identity(connection.url)
    if binding and (binding.provider != provider or server_identity(binding.server) != url):
        raise TrackerError("Repository binding targets a different tracker server; run ab-setup", "configuration")
    runtime_file = os.environ.get("AGENT_BOX_RUNTIME_CONFIG")
    if runtime_file:
        url = read_json(Path(runtime_file)).get("tracker_urls", {}).get(provider, url)
    from .vikunja import VikunjaAdapter
    return VikunjaAdapter(url, connection.token, binding)
