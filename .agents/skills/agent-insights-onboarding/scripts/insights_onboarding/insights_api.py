"""Public Agent Insights project API client."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import httpx

from .errors import OnboardingError

_API_VERSION = "2025-05-15-preview"
_TERMINAL = {"succeeded", "failed", "cancelled", "canceled"}


class AgentInsightsClient:
    def __init__(
        self,
        *,
        project_endpoint: str,
        credential: Any,
        timeout_seconds: float = 60,
    ) -> None:
        self._base = project_endpoint.rstrip("/")
        self._credential = credential
        self._client = httpx.Client(timeout=timeout_seconds, follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AgentInsightsClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _send(self, *args: Any, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(*args, **kwargs)
        except httpx.TimeoutException as error:
            raise OnboardingError(
                "agent_insights_timeout",
                "Agent Insights API request timed out.",
            ) from error
        except httpx.RequestError as error:
            raise OnboardingError(
                "agent_insights_unavailable",
                "Agent Insights API request could not reach the project endpoint.",
            ) from error

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        expected: set[int] | None = None,
    ) -> tuple[int, Any]:
        token = self._credential.get_token("https://ai.azure.com/.default").token
        query = {"api-version": _API_VERSION, **(dict(params or {}))}
        response = self._send(
            method,
            f"{self._base}{path}",
            params=query,
            json=json_body,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        allowed = expected or {200}
        if response.status_code not in allowed:
            request_id = response.headers.get("x-ms-request-id") or response.headers.get(
                "request-id"
            )
            raise OnboardingError(
                "agent_insights_request_failed",
                "Agent Insights API request failed.",
                {
                    "method": method,
                    "path": path,
                    "status": response.status_code,
                    "request_id": request_id,
                },
            )
        if response.status_code == 204 or not response.content:
            return response.status_code, None
        try:
            return response.status_code, response.json()
        except ValueError as error:
            raise OnboardingError(
                "invalid_agent_insights_response",
                "Agent Insights API returned malformed JSON.",
                {"method": method, "path": path, "status": response.status_code},
            ) from error

    def probe(self) -> dict[str, Any]:
        try:
            status, payload = self._request(
                "GET",
                "/agent_insight_monitors",
                params={"limit": 1},
            )
            return {"reachable": True, "authorized": status == 200, "payload": payload}
        except OnboardingError as error:
            raw_status = error.details.get("status")
            status = raw_status if isinstance(raw_status, int) else 0
            if status == 404:
                raise OnboardingError(
                    "feature_unavailable",
                    "Agent Insights is not enabled for the selected project subscription.",
                ) from error
            if status == 403:
                return {"reachable": True, "authorized": False}
            raise

    def list_monitors(self, agent_name: str) -> list[Mapping[str, Any]]:
        _, payload = self._request(
            "GET",
            "/agent_insight_monitors",
            params={"agent_name": agent_name, "limit": 2},
        )
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
            raise OnboardingError(
                "invalid_monitor_list",
                "Agent Insights monitor list response was invalid.",
            )
        return list(data)

    def get_monitor(self, monitor_id: str) -> Mapping[str, Any]:
        _, payload = self._request(
            "GET",
            f"/agent_insight_monitors/{monitor_id}",
        )
        if not isinstance(payload, Mapping):
            raise OnboardingError(
                "invalid_monitor",
                "Agent Insights monitor response was invalid.",
            )
        return payload

    def get_or_create_monitor(
        self,
        *,
        agent_name: str,
        model_deployment_name: str,
        run_interval_hours: float = 24,
    ) -> tuple[Mapping[str, Any], bool]:
        monitors = self.list_monitors(agent_name)
        if len(monitors) == 1:
            return monitors[0], False
        if len(monitors) > 1:
            raise OnboardingError(
                "ambiguous_monitor",
                "More than one monitor exists for the selected agent.",
            )
        _, payload = self._request(
            "POST",
            "/agent_insight_monitors",
            json_body={
                "agent_name": agent_name,
                "enabled": False,
                "run_interval_hours": run_interval_hours,
                "model_deployment_name": model_deployment_name,
            },
            expected={201},
        )
        if not isinstance(payload, Mapping) or not payload.get("id"):
            raise OnboardingError(
                "invalid_monitor",
                "Agent Insights monitor create response was invalid.",
            )
        return payload, True

    def create_run(self, monitor_id: str, lookback_hours: int = 168) -> Mapping[str, Any]:
        _, payload = self._request(
            "POST",
            f"/agent_insight_monitors/{monitor_id}/runs",
            json_body={"lookback_hours": lookback_hours},
            expected={201},
        )
        if not isinstance(payload, Mapping) or not payload.get("id"):
            raise OnboardingError(
                "invalid_insights_run",
                "Agent Insights run create response was invalid.",
            )
        return payload

    def list_runs(self, monitor_id: str) -> list[Mapping[str, Any]]:
        _, payload = self._request(
            "GET",
            f"/agent_insight_monitors/{monitor_id}/runs",
            params={"limit": 20, "order": "desc"},
        )
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
            raise OnboardingError(
                "invalid_run_list",
                "Agent Insights run list response was invalid.",
            )
        return list(data)

    def wait_run(
        self,
        *,
        monitor_id: str,
        run_id: str,
        timeout_seconds: float = 21600,
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            _, payload = self._request(
                "GET",
                f"/agent_insight_monitors/{monitor_id}/runs/{run_id}",
            )
            if not isinstance(payload, Mapping):
                raise OnboardingError(
                    "invalid_insights_run",
                    "Agent Insights run response was invalid.",
                )
            status = str(payload.get("status") or "").casefold()
            if status in _TERMINAL:
                if status != "succeeded":
                    error = payload.get("error")
                    code = (
                        str(error.get("code") or "")
                        if isinstance(error, Mapping)
                        else ""
                    )
                    raise OnboardingError(
                        "insights_run_failed",
                        f"Agent Insights run reached terminal state '{status}'.",
                        {"run_id": run_id, "service_error_code": code},
                    )
                return payload
            if time.monotonic() >= deadline:
                raise OnboardingError(
                    "insights_run_timeout",
                    "Agent Insights run did not finish before timeout.",
                    {"run_id": run_id},
                )
            time.sleep(30)

    def wait_new_scheduled_run(
        self,
        *,
        monitor_id: str,
        excluded_run_ids: set[str],
        timeout_seconds: float = 300,
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            for run in self.list_runs(monitor_id):
                run_id = str(run.get("id") or "")
                trigger = str(run.get("trigger") or "").casefold()
                if run_id and run_id not in excluded_run_ids and trigger == "scheduled":
                    return run
            if time.monotonic() >= deadline:
                raise OnboardingError(
                    "scheduled_run_admission_timeout",
                    "The monitor was enabled, but its immediate scheduled run was not "
                    "admitted before the timeout.",
                    {"monitor_id": monitor_id},
                )
            time.sleep(5)

    def list_insights(
        self,
        monitor_id: str,
        *,
        include_details: bool = False,
    ) -> list[Mapping[str, Any]]:
        _, payload = self._request(
            "GET",
            f"/agent_insight_monitors/{monitor_id}/insights",
            params={
                "limit": 20,
                "order": "desc",
                "include_details": str(include_details).lower(),
            },
        )
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
            raise OnboardingError(
                "invalid_insights_list",
                "Agent Insights list response was invalid.",
            )
        return list(data)

    def enable_monitor(self, monitor_id: str) -> Mapping[str, Any]:
        _, payload = self._request(
            "PATCH",
            f"/agent_insight_monitors/{monitor_id}",
            json_body={"enabled": True},
        )
        if not isinstance(payload, Mapping) or payload.get("enabled") is not True:
            raise OnboardingError(
                "monitor_enable_failed",
                "Agent Insights monitor did not become enabled.",
            )
        return payload

    def disable_monitor(self, monitor_id: str) -> Mapping[str, Any]:
        _, payload = self._request(
            "PATCH",
            f"/agent_insight_monitors/{monitor_id}",
            json_body={"enabled": False},
        )
        if not isinstance(payload, Mapping) or payload.get("enabled") is not False:
            raise OnboardingError(
                "monitor_disable_failed",
                "Agent Insights monitor did not become disabled.",
            )
        return payload

    def delete_monitor(self, monitor_id: str) -> None:
        self._request(
            "DELETE",
            f"/agent_insight_monitors/{monitor_id}",
            expected={204},
        )
