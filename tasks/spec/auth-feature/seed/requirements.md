# Requirements: JWT Authentication Feature

## Overview
Implement a JWT-based authentication system with user registration, access tokens, and refresh tokens.

## User Stories

- As a new user, I want to register with my email and password so that I can create an account.
- As a user, I want to log in with my email and password so that I receive a JWT access token.
- As a user, I want to use a refresh token to get a new access token without re-entering my credentials.
- As a user, I want to log out so that my refresh token is invalidated.
- As a developer, I want protected endpoints to reject requests without a valid JWT.

## Functional Requirements

WHEN a new user submits a unique email and password THE SYSTEM SHALL create the account and return a 201 Created response.
WHEN a user attempts to register with an email that already exists THE SYSTEM SHALL return a 409 Conflict response.
WHEN a user submits valid credentials THE SYSTEM SHALL return a signed JWT access token and a refresh token.
WHEN a user submits invalid credentials THE SYSTEM SHALL return a 401 Unauthorized response.
WHEN a client sends a valid refresh token THE SYSTEM SHALL issue a new access token.
WHEN a client sends an expired or invalid refresh token THE SYSTEM SHALL return a 401 Unauthorized response.
WHEN a user logs out THE SYSTEM SHALL invalidate the refresh token so it cannot be reused.
WHEN a request is made to a protected endpoint without a valid JWT THE SYSTEM SHALL return a 401 Unauthorized response.
WHEN a JWT access token expires THE SYSTEM SHALL return a 401 response prompting the client to refresh.

## Acceptance Criteria

- [ ] POST /auth/register accepts {email, password}, creates the user, and returns 201 (409 if the email already exists)
- [ ] POST /auth/login accepts {email, password} and returns {access_token, refresh_token, expires_in}
- [ ] POST /auth/refresh accepts {refresh_token} and returns a new {access_token}
- [ ] POST /auth/logout accepts {refresh_token} and invalidates it
- [ ] GET /auth/me returns the current user's profile when a valid JWT is provided
- [ ] Access tokens expire after 15 minutes
- [ ] Refresh tokens expire after 7 days
- [ ] Passwords are stored as bcrypt hashes

## Non-Functional Requirements

- **Security**: Tokens must be signed with HS256 or RS256. Secrets must not be hardcoded. Passwords must never be returned in any response.
- **Performance**: Login endpoint must respond within 500ms under normal load.
- **Reliability**: Token validation must not make external network calls.
