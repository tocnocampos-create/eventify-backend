# Eventify Auth API — Mobile Integration Guide

Base URL: `{API_HOST}/api`

All request/response bodies are `application/json`.

---

## POST `/auth/register`

Creates a new user account. No authentication required.

**Request body:**

```json
{
  "email": "user@example.com",
  "full_name": "John Doe",
  "password": "mysecurepassword"
}
```

| Field       | Type   | Required | Notes                    |
|-------------|--------|----------|--------------------------|
| email       | string | yes      | Must be a valid email    |
| full_name   | string | no       | User display name        |
| password    | string | yes      | Minimum 8 characters     |

**Success response — `201 Created`:**

```json
{
  "email": "user@example.com",
  "full_name": "John Doe",
  "id": 1,
  "role": "user",
  "is_active": true,
  "created_at": "2026-02-15T12:00:00+00:00"
}
```

**Error responses:**

| Status | Detail                   | When                        |
|--------|--------------------------|-----------------------------|
| 400    | `Email already registered` | Email is already in use    |
| 422    | Validation error         | Missing fields or password < 8 chars |

---

## POST `/auth/login`

Authenticates a user and returns a token pair. No authentication required.

**Request body:**

```json
{
  "email": "user@example.com",
  "password": "mysecurepassword"
}
```

**Success response — `200 OK`:**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer"
}
```

**Error responses:**

| Status | Detail                   | When                          |
|--------|--------------------------|-------------------------------|
| 401    | `Invalid email or password` | Wrong credentials          |
| 403    | `Inactive user`          | Account has been deactivated  |

---

## POST `/auth/refresh`

Exchanges a valid refresh token for a new token pair. No authentication header required — the refresh token is sent in the body.

**Request body:**

```json
{
  "refresh_token": "eyJhbGciOiJIUzI1NiIs..."
}
```

**Success response — `200 OK`:**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer"
}
```

Both tokens are new — store and replace the previous pair.

**Error responses:**

| Status | Detail                                       | When                              |
|--------|----------------------------------------------|-----------------------------------|
| 401    | `Invalid or expired refresh token`           | Token is malformed or expired     |
| 401    | `Invalid token type, refresh token required` | An access token was sent instead  |
| 401    | `User not found or inactive`                 | User was deleted or deactivated   |

---

## GET `/auth/me`

Returns the current user's profile. Requires authentication.

**Headers:**

```
Authorization: Bearer <access_token>
```

**Success response — `200 OK`:**

```json
{
  "email": "user@example.com",
  "full_name": "John Doe",
  "id": 1,
  "role": "user",
  "is_active": true,
  "created_at": "2026-02-15T12:00:00+00:00"
}
```

---

## Token Lifecycle

| Token        | Lifetime | Purpose                              |
|--------------|----------|--------------------------------------|
| access_token | 30 min   | Sent in `Authorization` header for all protected endpoints |
| refresh_token| 7 days   | Used only with `/auth/refresh` to get a new pair           |

## Using Tokens on Protected Endpoints

Every endpoint outside of `/auth/register`, `/auth/login`, and `/auth/refresh` requires the access token:

```
Authorization: Bearer <access_token>
```

If the access token is expired, the API returns `401`. The mobile app should then call `/auth/refresh` with the stored refresh token. If the refresh token is also expired, redirect the user to the login screen.

## Recommended Mobile Flow

```
1. Register or Login
   └─► Store access_token + refresh_token securely (Keychain / EncryptedSharedPreferences)

2. API calls
   └─► Attach header: Authorization: Bearer <access_token>

3. On 401 response
   ├─► Call POST /auth/refresh with stored refresh_token
   │   ├─► Success: replace both tokens, retry the original request
   │   └─► 401: refresh token expired → redirect to login
   └─► Never retry more than once to avoid loops

4. On logout
   └─► Delete both tokens from local storage
```

## Roles

| Role    | Read endpoints (GET) | Write endpoints (POST/PUT/DELETE) |
|---------|----------------------|-----------------------------------|
| `user`  | yes                  | no (403)                          |
| `admin` | yes                  | yes                               |

New accounts registered via `/auth/register` are always assigned the `user` role.
