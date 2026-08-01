"""Website authentication routes and request guards."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket

from rdk_maze_tuner.platform.auth import (
    AuthenticationError,
    AuthService,
    LoginRateLimiter,
    RateLimitExceeded,
    SessionPrincipal,
    UsernamePolicyError,
)


SESSION_COOKIE_NAME = "maze_session"
CSRF_HEADER_NAME = "X-CSRF-Token"


@dataclass(frozen=True)
class AuthContext:
    service: AuthService
    rate_limiter: LoginRateLimiter

    def require_principal(self, request: Request) -> SessionPrincipal:
        return self._resolve(request.cookies.get(SESSION_COOKIE_NAME))

    def require_state_change(self, request: Request) -> SessionPrincipal:
        principal = self.require_principal(request)
        if not self.service.verify_csrf(
            principal,
            request.headers.get(CSRF_HEADER_NAME),
        ):
            raise HTTPException(status_code=403, detail="valid CSRF token required")
        return principal

    def websocket_principal(self, websocket: WebSocket) -> SessionPrincipal:
        return self._resolve(websocket.cookies.get(SESSION_COOKIE_NAME))

    def _resolve(self, session_token: str | None) -> SessionPrincipal:
        try:
            return self.service.resolve_session(session_token)
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=401,
                detail="authentication required",
                headers={"WWW-Authenticate": "Session"},
            ) from exc


def create_auth_router(context: AuthContext) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.post("/login")
    async def login(request: Request, response: Response) -> dict:
        body = await _json_body(request)
        username = body.get("username") if isinstance(body.get("username"), str) else ""
        password = body.get("password") if isinstance(body.get("password"), str) else ""
        client_host = request.client.host if request.client is not None else "unknown"
        rate_key = f"{client_host}:{username.strip().casefold()}"
        try:
            context.rate_limiter.check(rate_key)
        except RateLimitExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail="too many login attempts",
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        try:
            credentials = context.service.login(username, password)
        except (AuthenticationError, UsernamePolicyError) as exc:
            context.rate_limiter.record_failure(rate_key)
            raise HTTPException(
                status_code=401,
                detail="invalid username or password",
            ) from exc

        context.rate_limiter.record_success(rate_key)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=credentials.session_token,
            max_age=context.service.session_ttl_seconds,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return {
            "user": {
                "user_id": credentials.user.user_id,
                "username": credentials.user.username,
            },
            "csrf_token": credentials.csrf_token,
            "expires_at": _utc_text(credentials.expires_at),
        }

    @router.post("/logout")
    def logout(request: Request, response: Response) -> dict:
        principal = context.require_state_change(request)
        context.service.logout(principal)
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return {"ok": True}

    @router.get("/me")
    def me(request: Request) -> dict:
        principal = context.require_principal(request)
        return {"user": _public_user(principal)}

    @router.get("/authorize")
    def authorize(request: Request) -> dict:
        principal = context.require_principal(request)
        return {"authorized": True, "user": _public_user(principal)}

    return router


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _public_user(principal: SessionPrincipal) -> dict:
    return {
        "user_id": principal.user_id,
        "username": principal.username,
    }


def _utc_text(value) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
