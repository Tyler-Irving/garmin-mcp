"""OAuth 2.1 authorisation server with PKCE and Dynamic Client Registration.

Single-user model: one shared password (set via ``MCP_AUTH_PASSWORD``) gates
every authorisation. Tokens are JWTs signed with ``JWT_SECRET``. Refresh
tokens rotate on every use.

This intentionally trades durability for simplicity: clients, codes, and
refresh tokens live in process memory and disappear on restart. Access
tokens survive restarts because they are stateless JWTs. For a single-user
personal server this is fine: the user simply re-authorises every cold
start of the OAuth flow.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import jwt
import structlog
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

log = structlog.get_logger(__name__)

ACCESS_TOKEN_TTL_SECONDS = 24 * 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
AUTHORIZATION_CODE_TTL_SECONDS = 10 * 60
LOGIN_STATE_TTL_SECONDS = 10 * 60


class InvalidLoginError(Exception):
    """Raised when /login receives the wrong password or a bad state value."""


class SimpleOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """Single-user OAuth server backed by a shared password and JWT secret."""

    def __init__(self, mcp_password: str, jwt_secret: str, issuer_url: str) -> None:
        if not mcp_password:
            raise ValueError("mcp_password must be a non-empty string.")
        if not jwt_secret or len(jwt_secret) < 16:
            raise ValueError("jwt_secret must be at least 16 characters.")
        self._password = mcp_password
        self._jwt_secret = jwt_secret
        self._issuer_url = issuer_url.rstrip("/")

        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        # state -> (client_id, params, created_at)
        self._pending_logins: dict[str, tuple[str, AuthorizationParams, float]] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        client_id = client_info.client_id
        if client_id is None:
            raise ValueError("Registered client must have a client_id assigned by the SDK.")
        self._clients[client_id] = client_info
        log.info(
            "oauth.client.registered",
            client_id=client_id,
            client_name=client_info.client_name,
            redirect_uris=[str(u) for u in (client_info.redirect_uris or [])],
        )

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        client_id = client.client_id
        if client_id is None:
            raise ValueError("Client passed to authorize() has no client_id.")
        self._sweep_pending_logins()
        state = secrets.token_urlsafe(24)
        self._pending_logins[state] = (client_id, params, time.monotonic())
        return f"{self._issuer_url}/login?state={state}"

    async def complete_login(self, state: str, password: str) -> str:
        """Finish the password challenge started by ``authorize``.

        Called by the /login POST handler. Returns the redirect URL the user
        should be sent to (their client's redirect_uri with code and state).
        """
        if not secrets.compare_digest(password, self._password):
            raise InvalidLoginError("Incorrect password.")

        self._sweep_pending_logins()
        record = self._pending_logins.pop(state, None)
        if record is None:
            raise InvalidLoginError("This login link has expired. Start the connection again.")

        client_id, params, _ = record
        code_value = secrets.token_urlsafe(32)
        self._codes[code_value] = AuthorizationCode(
            code=code_value,
            scopes=params.scopes or [],
            expires_at=time.time() + AUTHORIZATION_CODE_TTL_SECONDS,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return construct_redirect_uri(
            str(params.redirect_uri),
            code=code_value,
            state=params.state,
        )

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None:
            return None
        if code.client_id != client.client_id:
            return None
        if code.expires_at <= time.time():
            self._codes.pop(authorization_code, None)
            return None
        return code

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        # PKCE is verified by the SDK before this is called.
        client_id = self._require_client_id(client)
        self._codes.pop(authorization_code.code, None)

        access_token = self._mint_jwt(
            client_id,
            authorization_code.scopes,
            authorization_code.resource,
        )
        refresh_value = secrets.token_urlsafe(32)
        self._refresh_tokens[refresh_value] = RefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + REFRESH_TOKEN_TTL_SECONDS,
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh_value,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        rt = self._refresh_tokens.get(refresh_token)
        if rt is None or rt.client_id != client.client_id:
            return None
        if rt.expires_at is not None and rt.expires_at <= int(time.time()):
            self._refresh_tokens.pop(refresh_token, None)
            return None
        return rt

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotation: invalidate the presented refresh token immediately.
        client_id = self._require_client_id(client)
        self._refresh_tokens.pop(refresh_token.token, None)

        new_scopes = scopes or refresh_token.scopes
        access_token = self._mint_jwt(client_id, new_scopes, None)
        new_refresh = secrets.token_urlsafe(32)
        self._refresh_tokens[new_refresh] = RefreshToken(
            token=new_refresh,
            client_id=client_id,
            scopes=new_scopes,
            expires_at=int(time.time()) + REFRESH_TOKEN_TTL_SECONDS,
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=new_refresh,
            scope=" ".join(new_scopes) if new_scopes else None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                self._jwt_secret,
                algorithms=["HS256"],
                audience=self._issuer_url,
                issuer=self._issuer_url,
            )
        except jwt.PyJWTError as exc:
            log.debug("oauth.access_token.invalid", error=str(exc))
            return None

        return AccessToken(
            token=token,
            client_id=payload.get("sub", ""),
            scopes=list(payload.get("scopes", [])),
            expires_at=int(payload["exp"]) if "exp" in payload else None,
            resource=payload.get("resource"),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            self._refresh_tokens.pop(token.token, None)
        # Access tokens are stateless JWTs. We accept the revoke call so the
        # endpoint reports success, but the token will continue to validate
        # until it expires. For a single-user server this is acceptable.

    def _mint_jwt(
        self,
        client_id: str,
        scopes: list[str],
        resource: str | None,
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": client_id,
            "iss": self._issuer_url,
            "aud": self._issuer_url,
            "iat": now,
            "exp": now + ACCESS_TOKEN_TTL_SECONDS,
            "scopes": scopes or [],
        }
        if resource is not None:
            payload["resource"] = resource
        encoded = jwt.encode(payload, self._jwt_secret, algorithm="HS256")
        return encoded if isinstance(encoded, str) else encoded.decode("ascii")

    @staticmethod
    def _require_client_id(client: OAuthClientInformationFull) -> str:
        if client.client_id is None:
            raise ValueError("Client has no client_id.")
        return client.client_id

    def _sweep_pending_logins(self) -> None:
        cutoff = time.monotonic() - LOGIN_STATE_TTL_SECONDS
        stale = [s for s, (_, _, ts) in self._pending_logins.items() if ts < cutoff]
        for s in stale:
            self._pending_logins.pop(s, None)
