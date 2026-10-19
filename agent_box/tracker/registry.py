"""Explicit capabilities keep unimplemented products from looking usable."""
import os
from pathlib import Path

from ..atomicio import read_json

from ..config import IMPLEMENTED_TRACKERS, TRACKER_PROVIDERS, effective_config, session_account
from ..repo_binding import server_identity
from .core import TrackerError


def make_adapter(binding=None, provider=None, config=None):
    config = config or effective_config()
    provider = provider or (binding.provider if binding else config.tracker.provider)
    if not config.tracker.enabled:
        raise TrackerError("Tracker is disabled; enable it with agent-box config", "disabled")
    if not provider:
        raise TrackerError("No tracker provider selected; choose one with agent-box config, "
                           "in the project configuration, or with tracker --provider", "configuration")
    if provider not in TRACKER_PROVIDERS:
        raise TrackerError(f"Unknown tracker provider {provider}; choose {', '.join(TRACKER_PROVIDERS)}")
    if provider not in IMPLEMENTED_TRACKERS:
        raise TrackerError(f"Tracker provider '{provider}' is not implemented; configure Vikunja or GitHub with agent-box config",
                           "unsupported")
    connection = session_account(config).connection(provider)
    if not connection.usable:
        raise TrackerError(f"Configure the {provider} server and token with agent-box config", "configuration")
    url = server_identity(connection.url)
    expected = binding.server if binding else config.tracker.connection(provider).url if hasattr(config, "_project_base") else ""
    if (binding and binding.provider != provider) or (expected and server_identity(expected) != url):
        raise TrackerError("Repository binding targets a different tracker server; run ab-setup", "configuration")
    runtime_file = os.environ.get("AGENT_BOX_RUNTIME_CONFIG")
    if runtime_file:
        from ..runtime import container_tracker_url
        rendered = read_json(Path(runtime_file))
        if (rendered.get("container", True)
                and rendered.get("box", {}).get("network", {}).get("mode") != "host"):
            original = rendered.get("tracker", {}).get("connections", {}).get(provider, {}).get("url")
            if original and server_identity(original) == url:
                url = rendered.get("tracker_urls", {}).get(provider, url)
            else:
                url = container_tracker_url(url)
    if provider == "github":
        from .github import GitHubAdapter
        return GitHubAdapter(url, connection.token, binding)
    if provider == "jira":
        from .jira import JiraAdapter
        return JiraAdapter(url, connection.token, binding, username=connection.username)
    from .vikunja import VikunjaAdapter
    return VikunjaAdapter(url, connection.token, binding)
