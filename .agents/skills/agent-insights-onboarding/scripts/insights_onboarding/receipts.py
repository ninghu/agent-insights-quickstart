"""Atomic, secret-free receipt persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import OnboardingError

ResourceObserver = Callable[[str, Mapping[str, Any]], None]

_SECRET_KEY = re.compile(
    r"(?:access|refresh|identity)?token|authorization|connection.?string|"
    r"(?:api|account|client)?key|client.?secret|password|credential",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:Bearer\s+[A-Za-z0-9._~+/=-]{16,}|"
    r"InstrumentationKey\s*=|AccountKey\s*=|SharedAccessSignature\s*=|"
    r"EndpointSuffix\s*=|sig=[A-Za-z0-9%._~+/=-]{12,})",
    re.IGNORECASE,
)


def ensure_secret_free(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            if _SECRET_KEY.search(key):
                raise OnboardingError(
                    "secret_in_receipt",
                    f"Receipt field '{path}.{key}' is not allowed.",
                )
            ensure_secret_free(child, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            ensure_secret_free(child, f"{path}[{index}]")
        return
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        raise OnboardingError(
            "secret_in_receipt",
            f"Receipt value at '{path}' resembles a credential.",
        )


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    ensure_secret_free(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise OnboardingError(
            "receipt_not_found",
            f"Receipt does not exist: {path}",
        ) from error
    except json.JSONDecodeError as error:
        raise OnboardingError(
            "invalid_receipt",
            f"Receipt is not valid JSON: {path}",
        ) from error
    if not isinstance(value, dict):
        raise OnboardingError("invalid_receipt", f"Receipt must be an object: {path}")
    ensure_secret_free(value)
    return value


def verify_plan_payload(payload: Mapping[str, Any]) -> None:
    expected_hash = str(payload.get("plan_hash") or "")
    canonical = {key: value for key, value in payload.items() if key != "plan_hash"}
    actual_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if not expected_hash or actual_hash != expected_hash:
        raise OnboardingError(
            "plan_hash_mismatch",
            "Stored plan was modified after creation.",
        )


class ProvisioningJournal:
    """Record confirmed ownership before subsequent waits can fail."""

    def __init__(self, run_dir: Path, plan: Mapping[str, Any]) -> None:
        self.path = run_dir / "provisioning-state.json"
        previous = (
            self.path if self.path.exists() else run_dir / "provisioning-receipt.json"
        )
        self.payload: dict[str, Any] = (
            read_json(previous)
            if previous.exists()
            else {
                "schema_version": 2,
                "run_id": plan["run_id"],
                "plan_hash": plan["plan_hash"],
                "mode": plan["mode"],
                "created_role_assignments": [],
                "created_connection_ids": [],
            }
        )
        if (
            self.payload.get("run_id") != plan["run_id"]
            or self.payload.get("plan_hash") != plan["plan_hash"]
        ):
            raise OnboardingError(
                "journal_plan_mismatch",
                "Provisioning ownership journal does not match the frozen plan.",
            )
        assignments = self.payload.setdefault("created_role_assignments", [])
        connections = self.payload.setdefault("created_connection_ids", [])
        pending_connections = self.payload.setdefault("pending_connection_ids", [])
        pending_roles = self.payload.setdefault("pending_role_assignment_ids", [])
        if (
            not isinstance(assignments, list)
            or not all(isinstance(item, dict) and isinstance(item.get("id"), str)
                       for item in assignments)
            or not isinstance(connections, list)
            or not all(isinstance(item, str) for item in connections)
            or not isinstance(pending_connections, list)
            or not all(isinstance(item, str) for item in pending_connections)
            or not isinstance(pending_roles, list)
            or not all(isinstance(item, str) for item in pending_roles)
        ):
            raise OnboardingError(
                "invalid_journal", "Ownership journal has invalid resource lists."
            )
        self.payload["status"] = "in_progress"
        write_json_atomic(self.path, self.payload)

    def observe(self, kind: str, value: Mapping[str, Any]) -> None:
        if kind == "resource_group":
            self.payload["resource_group_id"] = str(value["id"])
        elif kind == "project":
            self.payload["project"] = dict(value)
        elif kind == "agent":
            previous = self.payload.get("agent")
            agent = dict(value)
            if isinstance(previous, dict):
                if any(previous.get(key) != agent.get(key) for key in ("name", "version", "kind")):
                    raise OnboardingError(
                        "agent_journal_mismatch",
                        "Resumed Agent differs from the version recorded for this run.",
                    )
                if not agent.get("artifact_sha256") and previous.get("artifact_sha256"):
                    agent["artifact_sha256"] = previous["artifact_sha256"]
            self.payload["agent"] = agent
            self.payload["agent_created"] = True
        elif kind == "connection":
            identifier = str(value["id"])
            identifiers = self.payload["created_connection_ids"]
            if identifier not in identifiers:
                identifiers.append(identifier)
            self.payload["pending_connection_ids"] = [
                item for item in self.payload["pending_connection_ids"]
                if item.casefold() != identifier.casefold()
            ]
        elif kind == "role_assignment":
            assignments = self.payload["created_role_assignments"]
            if not any(item["id"] == value["id"] for item in assignments):
                assignments.append(dict(value))
            self.payload["pending_role_assignment_ids"] = [
                item for item in self.payload["pending_role_assignment_ids"]
                if item.casefold() != str(value["id"]).casefold()
            ]
        elif kind in {"connection_pending", "role_assignment_pending"}:
            field = (
                "pending_connection_ids" if kind == "connection_pending"
                else "pending_role_assignment_ids"
            )
            if value["id"] not in self.payload[field]:
                self.payload[field].append(str(value["id"]))
        elif kind == "agent_pending":
            self.payload["agent_creation_started"] = True
        else:
            raise OnboardingError("invalid_journal_event", "Unknown resource journal event.")
        write_json_atomic(self.path, self.payload)
