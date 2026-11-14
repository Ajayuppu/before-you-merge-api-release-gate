"""Identity adapters.

The local adapter is an explicit simulation. The API Gateway adapter reads
requestContext.identity and never a reviewer supplied in the body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from apireview import LOCAL_SIMULATION_BANNER
from apireview.errors import AuthorizationFailed, ValidationFailed

_LOCAL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_ROLE_PATH = re.compile(
    r"^arn:aws[a-zA-Z-]*:sts::(?P<account>[0-9]{12}|example):assumed-role/(?P<role>[A-Za-z0-9+=,.@_-]+)/"
)
_IAM_ROLE = re.compile(
    r"^arn:aws[a-zA-Z-]*:iam::(?P<account>[0-9]{12}|example):role/(?P<role>[A-Za-z0-9+=,.@_-]+)$"
)
_IAM_USER = re.compile(
    r"^arn:aws[a-zA-Z-]*:iam::(?P<account>[0-9]{12}|example):user/(?P<user>[A-Za-z0-9+=,.@_-]+)$"
)

DEVELOPER_ROLE_NAME = "ApiReviewDeveloper"
REVIEWER_ROLE_NAME = "ApiReviewReviewer"


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str]
    source: str
    simulation: bool

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def public_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "subject": self.subject,
            "roles": sorted(self.roles),
            "source": self.source,
            "simulation": self.simulation,
        }
        if self.simulation:
            payload["banner"] = LOCAL_SIMULATION_BANNER
        return payload


def local_principal(name: str, roles: list[str] | tuple[str, ...]) -> Principal:
    if not isinstance(name, str) or not _LOCAL_NAME.fullmatch(name):
        raise ValidationFailed("local principal name must match [a-z][a-z0-9-]{0,31}")
    if name.startswith("arn") or "token" in name or name.startswith("akia"):
        raise ValidationFailed("local principal name is not an ARN or token")
    cleaned: set[str] = set()
    for role in roles:
        if role not in {"developer", "reviewer"}:
            raise ValidationFailed("local roles must be developer or reviewer")
        cleaned.add(role)
    if not cleaned:
        raise ValidationFailed("local principal needs at least one role")
    return Principal(
        subject=f"local:{name}",
        roles=frozenset(cleaned),
        source="local-simulation",
        simulation=True,
    )


def require_local_simulation_header(value: str | None) -> None:
    if value != "simulation":
        raise AuthorizationFailed(
            "local HTTP identity requires header X-Local-Auth: simulation. "
            + LOCAL_SIMULATION_BANNER
        )


def principal_from_api_gateway(
    event: dict,
    *,
    expected_role_name: str,
    simulation_account_allowed: bool,
) -> Principal:
    """Read the IAM caller from a REST API proxy event.

    AWS_IAM places the caller on requestContext.identity.userArn. Custom
    authorizer claims are intentionally ignored so a body or authorizer map
    cannot nominate the reviewer.
    """
    context = event.get("requestContext")
    if not isinstance(context, dict):
        raise AuthorizationFailed("missing requestContext")
    identity = context.get("identity")
    if not isinstance(identity, dict):
        raise AuthorizationFailed("missing requestContext.identity")
    user_arn = identity.get("userArn")
    if not isinstance(user_arn, str) or not user_arn:
        raise AuthorizationFailed("missing requestContext.identity.userArn")
    if "example" in user_arn and not simulation_account_allowed:
        raise AuthorizationFailed("example account ARNs are refused in production mode")
    role_name = _role_name(user_arn)
    if role_name != expected_role_name:
        raise AuthorizationFailed("caller IAM role is not allowed for this method")
    return Principal(
        subject=user_arn,
        roles=frozenset({_role_to_local(role_name)}),
        source="api-gateway-iam",
        simulation=simulation_account_allowed,
    )


def _role_name(user_arn: str) -> str:
    match = _ROLE_PATH.match(user_arn) or _IAM_ROLE.match(user_arn)
    if not match:
        if _IAM_USER.match(user_arn):
            raise AuthorizationFailed("IAM users are not granted method access; use the dedicated role")
        raise AuthorizationFailed("unsupported caller ARN shape")
    return match.group("role")


def _role_to_local(role_name: str) -> str:
    if role_name == DEVELOPER_ROLE_NAME:
        return "developer"
    if role_name == REVIEWER_ROLE_NAME:
        return "reviewer"
    raise AuthorizationFailed("caller IAM role is not allowed for this method")
