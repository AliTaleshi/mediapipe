import hmac
from typing import Annotated

from fastapi import Depends, Header, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, RateLimitedError, UnauthorizedError
from app.db.session import get_session
from app.services.auth import AuthContext, authenticate
from app.services.ratelimit import check_rate_limit
from app.services.redis import get_redis

bearer_scheme = HTTPBearer(
    auto_error=False, description="Project API key: `Authorization: Bearer mp_<prefix>_<secret>`"
)

Session = Annotated[AsyncSession, Depends(get_session)]


async def get_auth(
    request: Request,
    response: Response,
    session: Session,
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> AuthContext:
    """Authenticates the API key, applies the per-key rate limit and scopes the request."""
    auth = await authenticate(session, credentials.credentials if credentials else None)
    result = await check_rate_limit(redis, str(auth.api_key_id), settings.rate_limit_per_minute)
    if not result.allowed:
        raise RateLimitedError(
            "Rate limit exceeded.",
            code="rate_limited",
            details={"limit": result.limit, "retry_after_seconds": result.reset_seconds},
            headers={**result.headers(), "Retry-After": str(result.reset_seconds)},
        )
    response.headers.update(result.headers())
    request.state.auth = auth
    return auth


Auth = Annotated[AuthContext, Depends(get_auth)]


def require_admin(
    settings: Annotated[Settings, Depends(get_settings)],
    x_admin_token: Annotated[str | None, Header(description="Value of ADMIN_TOKEN")] = None,
) -> None:
    if not settings.admin_token:
        raise ForbiddenError("Admin API is disabled.", code="admin_disabled")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise UnauthorizedError("Invalid admin token.", code="invalid_admin_token")
