# Authentication

Authentication establishes who is calling. Session management carries that identity between requests. Authorization decides whether the caller may perform the use case. Design all three explicitly.

For a practical cross-cutting implementation, see the reference service's [`TokenVerifier` ABC](../examples/reference-service/src/app/authentication/application/token_verifier.py), [`LocalTokenVerifier`](../examples/reference-service/src/app/authentication/infrastructure/verifier.py), shared [`AuthenticatedUserId`](../examples/reference-service/src/app/authentication/presentation/dependencies.py), and central [`401` handler](../examples/reference-service/src/app/authentication/presentation/errors.py). The local opaque-token adapter exists only to keep the example self-contained; a deployed service replaces that adapter without changing consuming routes.

## Layer boundaries

Keep HTTP credential extraction in presentation. A dependency validates the credential and returns the smallest useful principal:

```python
async def authenticated_user_id(
    token: Annotated[str, Depends(access_token_cookie)],
    verifier: FromDishka[TokenVerifier],
) -> UUID:
    return verifier.verify(token=token).user_id

AuthenticatedUserId = Annotated[UUID, Depends(authenticated_user_id)]
```

Endpoints accept `AuthenticatedUserId`; application services accept a plain `UUID` or richer domain principal. Neither the service nor domain imports `Depends`, reads cookies, or raises `HTTPException`.

Do not accept an authenticated identity only to discard it. Pass it into every
application operation that reads or changes user-owned data, and include it in
the corresponding repository query. A request for another user's resource
should normally produce the same not-found result as an absent resource.

Authentication is cross-cutting presentation behavior. Define the dependency alias once and import that public alias in every protected feature router. Do not copy token extraction into individual endpoints or hide protection behind an undocumented router convention. Public routes such as health checks remain explicitly unauthenticated. See the reference task [`router.py`](../examples/reference-service/src/app/features/tasks/presentation/router.py) for shared use and declared `401` response schemas.

Model token creation/validation and identity-provider calls as application ports. Infrastructure implements them with a specific JWT or OAuth library. Keep cookie writing and deletion in a presentation helper because cookies are HTTP response behavior.

Use one dependency implementation for HTTP and WebSocket authentication semantics, but translate failures to the protocol-appropriate response (`401` versus a WebSocket policy close).

## Local users and external identities

Represent a local account independently from an external login:

```text
users                 auth_identities
─────                 ───────────────
id             <────  user_id
profile data          provider
                      subject
                      UNIQUE(provider, subject)
```

Use the provider's stable subject identifier as the external key. An email address is profile data, can change, and must not link accounts unless the provider's verification guarantees and the application's linking policy explicitly allow it.

Create the local user and identity in one transaction. Enforce `(provider, subject)` uniqueness in the database and handle concurrent first-login races.

Keep provider access/refresh credentials in a separate connection aggregate when they grant access to a third-party API. Encrypt them at rest and do not expose them through user API schemas.

## OAuth and OpenID Connect

- Use Authorization Code with PKCE; do not use the implicit grant.
- Generate a high-entropy, short-lived, single-use `state` and compare it on callback.
- Use exact registered redirect URIs and bind the response to the initiating provider/issuer.
- Exchange codes only on the server when the backend owns the session.
- Validate OIDC ID-token signature, issuer, audience, expiry, and nonce before trusting claims.
- Request only required scopes and do not treat provider access tokens as application session tokens.
- Return generic client errors while retaining sanitized diagnostic context server-side.

These rules follow the [OAuth 2.0 Security Best Current Practice (RFC 9700)](https://datatracker.ietf.org/doc/html/rfc9700).

## Application tokens

Prefer an opaque server-side session when immediate revocation and simple browser security matter more than decentralized verification. Use JWTs only when self-contained verification is a real requirement.

When using JWTs:

- allow-list the expected algorithm; never derive it from an untrusted header;
- validate signature, issuer, audience, expiry, not-before time, and required claims;
- include and validate an explicit token purpose (`access`, `refresh`, password reset, and so on);
- use short-lived access tokens and avoid sensitive claims in readable payloads;
- plan key rotation using key identifiers without accepting unknown algorithms;
- remember that expiry is not revocation—maintain session/version state where logout or compromise response requires it.

If refresh tokens exist, rotate them on use or sender-constrain them. Persist enough one-way token/session state to detect replay, revoke the affected token family, and force reauthentication. Make refresh operations atomic.

## Browser sessions and cookies

Prefer cookies with:

- `Secure` in every non-local environment;
- `HttpOnly` for tokens not needed by JavaScript;
- an explicit `SameSite=Lax` or `Strict` where the flow allows it;
- the narrowest workable `Path`, no broad `Domain`, and a bounded lifetime;
- a `__Host-` name when the cookie is secure, host-only, and scoped to `/`.

Do not store session identifiers, access tokens, or refresh tokens in `localStorage` or URLs. Return `Cache-Control: no-store` on responses carrying credentials.

`SameSite` is defense in depth, not a complete CSRF strategy. Protect cookie-authenticated state changes with a validated CSRF token or a comparably strong origin-bound design. CORS does not prevent CSRF. See the OWASP guidance on [session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) and [CSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).

Cookie deletion must repeat the same name, path, and domain scope used when setting the cookie. Logout revokes server-side session/refresh state before clearing client cookies.

## Authorization

Authenticate once at the boundary, then authorize at the use-case and resource boundary. Do not rely on a hidden UI control or a router prefix as authorization.

Repository contracts for user-owned resources accept the owner identity and
constrain the query with it. Application services decide the policy and pass
the authenticated actor explicitly. Prefer returning not-found for resources
whose existence should not be disclosed. Centralize role/scope vocabulary and
default to denial when policy data is missing.

## Errors and observability

Use `401` when authentication is absent or invalid and `403` when an authenticated principal lacks permission. Avoid revealing whether an account, provider subject, or reset token exists.

Log authentication outcomes with request/session correlation and stable reason codes, never raw cookies, authorization codes, secrets, or full tokens. Audit login, logout, refresh replay, account linking, privilege changes, and recovery events.

Rate-limit credential, callback, refresh, recovery, and verification endpoints based on a threat model. Reauthentication should be required for sensitive account operations.
