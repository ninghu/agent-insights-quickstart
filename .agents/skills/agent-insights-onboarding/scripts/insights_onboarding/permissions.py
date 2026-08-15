"""Least-privilege role planning and application."""

from __future__ import annotations

import fnmatch
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .azure_cli import AzureCli
from .errors import OnboardingError

PrincipalType = Literal["User", "ServicePrincipal"]


@dataclass(frozen=True, slots=True)
class Role:
    name: str
    definition_id: str


@dataclass(frozen=True, slots=True)
class RequiredAssignment:
    principal_id: str
    principal_type: PrincipalType
    role: Role
    scope: str

    @property
    def assignment_id(self) -> str:
        seed = "|".join(
            (
                self.scope.rstrip("/").casefold(),
                self.principal_id.casefold(),
                self.role.definition_id.casefold(),
            )
        )
        return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


FOUNDRY_USER = Role("Foundry User", "53ca6127-db72-4b80-b1b0-d745d6d5456d")
FOUNDRY_PROJECT_MANAGER = Role(
    "Foundry Project Manager",
    "eadc314b-1a2d-4efa-be10-5d325db5065e",
)
MONITORING_READER = Role(
    "Monitoring Reader",
    "43d0d8ad-25c7-4714-9337-8ba259a9fe05",
)
PRIVILEGED_MONITORING_DATA_READER = Role(
    "Privileged Monitoring Data Reader",
    "dbc9c667-e97f-4491-aee6-90b9cf960190",
)
COGNITIVE_SERVICES_OPENAI_USER = Role(
    "Cognitive Services OpenAI User",
    "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd",
)


def required_assignments(
    *,
    current_user_id: str,
    project_principal_id: str,
    foundry_account_id: str,
    project_id: str,
    application_insights_id: str,
    workspace_id: str,
    agent_type: str,
    protected_trace_content: bool,
    manage_hosted_agent: bool = True,
) -> list[RequiredAssignment]:
    user_role = (
        FOUNDRY_PROJECT_MANAGER
        if agent_type == "hosted" and manage_hosted_agent
        else FOUNDRY_USER
    )
    assignments = [
        RequiredAssignment(
            project_principal_id,
            "ServicePrincipal",
            COGNITIVE_SERVICES_OPENAI_USER,
            foundry_account_id,
        ),
        RequiredAssignment(
            project_principal_id,
            "ServicePrincipal",
            MONITORING_READER,
            application_insights_id,
        ),
        RequiredAssignment(current_user_id, "User", user_role, project_id),
        RequiredAssignment(
            current_user_id,
            "User",
            MONITORING_READER,
            application_insights_id,
        ),
    ]
    if protected_trace_content:
        assignments.extend(
            (
                RequiredAssignment(
                    project_principal_id,
                    "ServicePrincipal",
                    PRIVILEGED_MONITORING_DATA_READER,
                    workspace_id,
                ),
                RequiredAssignment(
                    current_user_id,
                    "User",
                    PRIVILEGED_MONITORING_DATA_READER,
                    workspace_id,
                ),
            )
        )
    return assignments


def role_guid(value: object) -> str:
    return str(value or "").rstrip("/").split("/")[-1].casefold()


def has_assignment(
    assignments: Iterable[Mapping[str, Any]],
    required: RequiredAssignment,
) -> bool:
    expected_principal = required.principal_id.casefold()
    expected_role = required.role.definition_id.casefold()
    for assignment in assignments:
        if (
            str(assignment.get("principalId") or "").casefold() == expected_principal
            and role_guid(assignment.get("roleDefinitionId")) == expected_role
        ):
            return True
    return False


def list_assignments(cli: AzureCli, required: RequiredAssignment) -> list[Mapping[str, Any]]:
    value = cli.json(
        [
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            required.principal_id,
            "--scope",
            required.scope,
            "--include-inherited",
        ]
    )
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise OnboardingError(
            "invalid_role_assignments",
            "Azure CLI returned an invalid role assignment list.",
        )
    return list(value)


def missing_assignments(
    cli: AzureCli,
    required: Sequence[RequiredAssignment],
) -> list[RequiredAssignment]:
    return [
        item for item in required if not has_assignment(list_assignments(cli, item), item)
    ]


def create_assignment(cli: AzureCli, required: RequiredAssignment) -> Mapping[str, Any]:
    value = cli.json(
        [
            "role",
            "assignment",
            "create",
            "--assignee-object-id",
            required.principal_id,
            "--assignee-principal-type",
            required.principal_type,
            "--role",
            required.role.definition_id,
            "--scope",
            required.scope,
            "--name",
            required.assignment_id,
        ]
    )
    if not isinstance(value, Mapping):
        raise OnboardingError(
            "invalid_role_assignment_result",
            "Azure returned an invalid role assignment create response.",
        )
    if (
        str(value.get("principalId") or "").casefold() != required.principal_id.casefold()
        or role_guid(value.get("roleDefinitionId"))
        != required.role.definition_id.casefold()
        or str(value.get("scope") or "").rstrip("/").casefold()
        != required.scope.rstrip("/").casefold()
    ):
        raise OnboardingError(
            "role_assignment_mismatch",
            "Created role assignment did not match the planned principal, role, and scope.",
        )
    return value


def action_allowed(
    permission_sets: Sequence[Mapping[str, Any]],
    required_action: str,
) -> bool:
    target = required_action.casefold()
    for permission_set in permission_sets:
        actions = [
            str(value).casefold() for value in permission_set.get("actions", []) or []
        ]
        denied = [
            str(value).casefold()
            for value in permission_set.get("notActions", []) or []
        ]
        if any(fnmatch.fnmatchcase(target, pattern) for pattern in actions) and not any(
            fnmatch.fnmatchcase(target, pattern) for pattern in denied
        ):
            return True
    return False


def require_actions(
    permission_sets: Sequence[Mapping[str, Any]],
    actions: Sequence[str],
    *,
    scope: str,
) -> None:
    missing = [action for action in actions if not action_allowed(permission_sets, action)]
    if missing:
        raise OnboardingError(
            "insufficient_preflight_permission",
            "The signed-in user lacks required effective Azure actions.",
            {"scope": scope, "missing_actions": missing},
        )
