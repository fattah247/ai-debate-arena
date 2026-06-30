"""Shared role metadata for the local arena.

The internal keys are intentionally stable because saved sessions and runtime
state use them. Display labels are generic so each prompt can define whatever
real-world role is needed for a run.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleDefinition:
    key: str
    label: str
    url_field: str
    prompt_field: str


ROLE_DEFINITIONS: tuple[RoleDefinition, ...] = (
    RoleDefinition(
        key="operator",
        label="Role 1",
        url_field="operator_url",
        prompt_field="operator_role",
    ),
    RoleDefinition(
        key="investor",
        label="Role 2",
        url_field="investor_url",
        prompt_field="investor_role",
    ),
    RoleDefinition(
        key="customer",
        label="Role 3",
        url_field="customer_url",
        prompt_field="customer_role",
    ),
    RoleDefinition(
        key="moderator",
        label="Moderator",
        url_field="moderator_url",
        prompt_field="moderator_role",
    ),
)

ROLE_KEYS = tuple(role.key for role in ROLE_DEFINITIONS)
ROLE_LABELS = {role.key: role.label for role in ROLE_DEFINITIONS}
PROMPT_FIELDS = tuple(role.prompt_field for role in ROLE_DEFINITIONS)
URL_FIELDS = tuple(role.url_field for role in ROLE_DEFINITIONS)

ARENA_MODE_LABELS = {
    "two_ai": "2 AI: Role 1 + Role 2",
    "three_ai": "3 AI: Role 1 + Role 2 + Moderator",
    "four_ai": "4 AI: Role 1 + Role 2 + Role 3 + Moderator",
}


def active_roles_for_mode(arena_mode: str) -> list[str]:
    roles = ["operator", "investor"]

    if arena_mode == "four_ai":
        roles.append("customer")

    if arena_mode in {"three_ai", "four_ai"}:
        roles.append("moderator")

    return roles
