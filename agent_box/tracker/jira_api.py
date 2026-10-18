"""Jira transport keeps Cloud search separate from the Server/DC REST contract."""
from __future__ import annotations

import base64
import http.client
import json
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

from .core import TrackerError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Credentials belong only to the configured endpoint.
        return None


class JiraClient:
    def __init__(self, url, token, username="", timeout=30, opener=None):
        self.url = url.rstrip("/")
        parts = urlsplit(self.url)
        if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.query or parts.fragment:
            raise TrackerError("Use a Jira site base URL without credentials, query or fragment", "configuration")
        self.token, self.username = token, username
        self.auth = ("Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()
                     if username else "Bearer " + token)
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open
        self._cloud = None

    def redact(self, value):
        text = str(value)
        for secret in (self.auth, self.auth.split(" ", 1)[1], self.token):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text

    def error(self, status, write=False):
        unknown = write and (status in (0, 408) or status >= 500 or 200 <= status < 300)
        category = ("unknown-outcome" if unknown else "authentication" if status in (401, 403)
                    else "not-found" if status == 404 else "conflict" if status == 409
                    else "transport" if status in (0, 408, 429) or status >= 500 else "provider")
        # Response bodies may echo arbitrary submitted fields, including credentials.
        message = f"Jira API HTTP {status}" if status else "Jira transport request failed"
        if unknown:
            message += "; write outcome unknown. Reread task state/comments before repeating the write"
        return TrackerError(message, category, category == "transport")

    def request(self, method, path, body=None, params=None):
        url = self.url + "/rest/api/2" + path
        if params:
            url += "?" + urlencode(params)
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": self.auth, "Accept": "application/json",
                                              "Content-Type": "application/json", "User-Agent": "agent-box-tracker/1"})
        write = method not in {"GET", "HEAD"}
        try:
            with self.opener(req, timeout=self.timeout) as response:
                status, payload = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                problem = json.loads(exc.read().decode())
                detail = ("; ".join(str(item) for item in problem.get("errorMessages", [])) + " " +
                          "; ".join(f"{key}: {value}" for key, value in problem.get("errors", {}).items())).strip()
            except (OSError, http.client.HTTPException, ValueError, AttributeError, UnicodeError):
                detail = ""
            exc.close()
            error = self.error(status, write)
            if detail:
                error = TrackerError(f"{error}: {self.redact(detail)[:1000]}", error.category, error.retryable)
            raise error from None
        except (OSError, http.client.HTTPException):
            raise self.error(0, write) from None
        if not payload and status in (201, 204, 205):
            return None
        try:
            return json.loads(payload.decode())
        except (UnicodeError, ValueError):
            raise self.error(status, write) from None

    @property
    def cloud(self):
        if self._cloud is None:
            cloud = self.request("GET", "/serverInfo").get("deploymentType", "").casefold() == "cloud"
            if cloud and not self.username:
                raise TrackerError("Jira Cloud requires an email username and API token", "configuration")
            self._cloud = cloud
        return self._cloud

    def pages(self, path, key, params=None):
        start, result = 0, []
        for _ in range(10000):
            page = self.request("GET", path, params={**(params or {}), "startAt": start, "maxResults": 100})
            rows = page[key]
            result.extend(rows)
            start += len(rows)
            if page.get("isLast") is True or start >= page.get("total", float("inf")):
                return result
            if not rows:
                raise TrackerError("Jira pagination ended before all results were read", "provider")
        raise TrackerError("Jira pagination did not converge", "provider")

    def search(self, jql):
        if not self.cloud:
            return self.pages("/search", "issues", {"jql": jql, "fields": "*all"})
        result, cursor, seen = [], None, set()
        for _ in range(10000):
            params = {"jql": jql, "fields": "summary", "maxResults": 100}
            if cursor:
                params["nextPageToken"] = cursor
            page = self.request("GET", "/search/jql", params=params)
            result.extend(page["issues"])
            if page.get("isLast") is True:
                return result
            cursor = page.get("nextPageToken")
            if not cursor or cursor in seen:
                raise TrackerError("Jira Cloud search returned incomplete pagination", "provider")
            seen.add(cursor)
        raise TrackerError("Jira Cloud search pagination did not converge", "provider")
