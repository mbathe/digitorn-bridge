---
id: auth
title: Authentication and Authorization
sidebar_position: 22
---

# Authentication and Authorization

Digitorn provides a complete, provider-agnostic authentication system with JWT tokens, role-based access control (RBAC), and pluggable identity providers. Designed for enterprise environments where security, auditability, and multi-provider support are requirements.

## Architecture Overview

```mermaid
flowchart TB
    subgraph Clients
        CLI[CLI Client]
        WEB[Web App]
        SDK[SDK / M2M]
    end

    subgraph Auth["Authentication Layer"]
        MW[Auth Middleware]
        JWT[JWT Service]
        AS[Auth Service]
    end

    subgraph Providers["Identity Providers"]
        LP[Local Provider\nbcrypt + DB]
        LDAP[LDAP Provider\nActive Directory]
        OA[OAuth2 Provider\nGoogle / Azure / GitHub]
        AK[API Key Provider\nMachine-to-Machine]
    end

    subgraph Storage["Persistence"]
        DB[(SQLAlchemy DB)]
        KV[(Session Store\nDiskCache / Redis)]
    end

    CLI -->|Bearer Token| MW
    WEB -->|Bearer Token| MW
    SDK -->|X-API-Key| MW

    MW -->|Verify| JWT
    MW -->|API Key| AK
    JWT -->|Decode| AS

    AS --> LP
    AS --> LDAP
    AS --> OA
    AS --> AK

    LP --> DB
    LDAP -->|Auto-provision| DB
    OA -->|Auto-provision| DB
    AK --> DB

    AS -->|Sessions| KV
```

## Authentication Flow

```mermaid
sequenceDiagram
    participant U as User
    participant API as Digitorn API
    participant AS as Auth Service
    participant P as Provider
    participant DB as Database

    U->>API: POST /auth/login {username, password}
    API->>AS: login(credentials)
    AS->>P: authenticate(credentials)
    P->>DB: Lookup user
    DB-->>P: User record
    P-->>AS: AuthResult (success)
    AS->>DB: Store refresh token (hashed)
    AS->>AS: Generate JWT (access + refresh)
    AS-->>API: LoginResult
    API-->>U: {access_token, refresh_token, expires_in}

    Note over U,API: Subsequent requests use Bearer token

    U->>API: POST /chat/stream<br/>Authorization: Bearer {token}
    API->>AS: verify_access_token(token)
    AS-->>API: TokenPayload {user_id, roles}
    API->>API: Inject user_id into session
    API-->>U: Socket.IO stream (agent response)

    Note over U,API: Token refresh (every 15 min)

    U->>API: POST /auth/refresh {refresh_token}
    API->>AS: refresh(token)
    AS->>DB: Verify refresh token not revoked
    AS-->>API: New access_token
    API-->>U: {access_token, expires_in}
```

## Configuration

Authentication is configured in the server configuration file, not in individual app YAML files. This is intentional: auth is an infrastructure concern, not an application concern.

```yaml
# server.yaml or environment variables
server:
  auth:
    enabled: true
    secret_key: "{{env.JWT_SECRET}}"      # auto-generated if omitted
    access_token_ttl: 900                  # 15 minutes (seconds)
    refresh_token_ttl: 604800              # 7 days (seconds)

    providers:
      # Local provider (default) — username/password in DB
      - type: local
        default: true

      # LDAP / Active Directory
      - type: ldap
        config:
          server: "ldap://ad.company.com"
          base_dn: "dc=company,dc=com"
          bind_dn: "{{env.LDAP_BIND_DN}}"
          bind_password: "{{env.LDAP_BIND_PASSWORD}}"
          user_search: "(sAMAccountName={username})"
          auto_provision: true

      # OAuth2 (Google, GitHub, Azure, custom)
      - type: oauth2
        config:
          provider: google
          client_id: "{{env.GOOGLE_CLIENT_ID}}"
          client_secret: "{{env.GOOGLE_CLIENT_SECRET}}"
          redirect_uri: "http://localhost:8000/auth/callback"

      # API keys for machine-to-machine
      - type: api_key
        config:
          header: "X-API-Key"
```
When `auth.enabled` is false (default), all requests pass through without authentication. The `user_id` is set to `"anonymous"`.

## Identity Providers

### Local Provider

The default provider. Users register with a username and password. Passwords are hashed with bcrypt (12 rounds, constant-time comparison).

```
POST /auth/register
{
  "username": "john",
  "password": "secure_password_123",
  "email": "john@company.com",
  "display_name": "John Doe"
}
```

New users are assigned the `developer` role by default.

### LDAP / Active Directory

Authenticates against an LDAP directory. On first login, the user record is automatically created in the local database (auto-provisioning). Subsequent logins update the email and display name from the directory.

The authentication process:
1. Service account connects to LDAP and searches for the user DN
2. A separate bind attempt verifies the user's password
3. User attributes (email, display name) are extracted
4. Local user record is created or updated

Supports Active Directory, OpenLDAP, and FreeIPA.

### OAuth2 / OIDC

Supports authorization code flow with well-known configurations for Google, GitHub, and Azure AD. Any OIDC-compliant provider can be used with custom URLs.

The flow:
1. `GET /auth/oauth/{provider}` redirects the user to the identity provider
2. User authorizes the application
3. Callback receives an authorization code
4. Code is exchanged for an access token
5. User info is fetched from the provider
6. Local user record is created or updated

### API Key

For machine-to-machine integrations (CI/CD, scripts, SDK clients). API keys are generated with `dk_` prefix, hashed with SHA-256, and stored in the database.

```
# API key format
dk_{prefix}_{random_48_chars}

# Usage
curl -H "X-API-Key: dk_a1b2c3d4_..." https://api.example.com/apps
```

Keys can be scoped to specific applications and have expiration dates.

## Role-Based Access Control

```mermaid
flowchart LR
    subgraph Roles
        A[admin]
        D[developer]
        V[viewer]
        C[custom roles]
    end

    subgraph Permissions
        P1["*"]
        P2[apps:read\napps:write\napps:deploy]
        P3[sessions:read\nsessions:write]
        P4[agents:run\nagents:spawn]
        P5[mcp:read\nmcp:install]
    end

    A --> P1
    D --> P2
    D --> P3
    D --> P4
    D --> P5
    V -->|read only| P2
    V -->|read only| P3
    C -->|configurable| P2
```

### Built-in Roles

| Role | Permissions | Description |
|------|------------|-------------|
| `admin` | `*` (all) | Full access to all features, users, and configuration |
| `developer` | apps, sessions, agents, MCP | Create and manage apps, run agents, view sessions |
| `viewer` | apps:read, sessions:read | Read-only access to apps and sessions |

### Custom Roles

Custom roles can be created via the admin API with any combination of permissions:

```
POST /admin/roles
{
  "name": "data_analyst",
  "description": "Can run apps and view sessions but not deploy",
  "permissions": ["apps:read", "sessions:read", "sessions:write", "agents:run"]
}
```

Users can have multiple roles. Permissions are merged (union of all role permissions).

## Session Management

Every conversation session is bound to a `user_id`. Users can only see and manage their own sessions.

### Session Persistence

Messages are automatically persisted after each agent turn. When a user returns to a session, the full message history is reloaded.

```mermaid
sequenceDiagram
    participant U as User
    participant API as API
    participant SS as Session Store
    participant M as Memory Module

    Note over U: Day 1 - Start working
    U->>API: POST /chat {session_id: "abc", message: "Fix the auth bug"}
    API->>SS: Load or create session
    API->>API: Run agent turn
    API->>SS: Save messages
    API->>M: Persist memory (goal, tasks, facts)

    Note over U: Day 2 - Resume
    U->>API: POST /chat {session_id: "abc", message: "Did you fix it?"}
    API->>SS: Load persisted messages
    API->>M: Restore memory
    API->>API: Agent sees full history + memory
    API-->>U: "Yes, I fixed the bug yesterday. Here's what I did..."
```

### Session Resume

When a session is resumed, the agent receives:
- The full message history (user messages, agent responses, tool calls)
- The memory snapshot (goal, plan, tasks, facts, notes, checkpoints)
- The original request (preserved verbatim)

This means the agent continues exactly where it left off, even across daemon restarts.

### Session Fork

Forking creates an independent copy of a session. The original session continues unchanged. Use this to explore alternative approaches:

```
POST /auth/sessions/abc123/fork
{"new_session_id": "abc123-alt"}
```

Both sessions have the same message history up to the fork point. After forking, they diverge independently.

## API Reference

### Authentication Endpoints

| Method | Path | Description | Auth Required |
|--------|------|-------------|---------------|
| POST | `/auth/register` | Create local user account | No |
| POST | `/auth/login` | Authenticate and receive tokens | No |
| POST | `/auth/refresh` | Exchange refresh token | No |
| POST | `/auth/logout` | Revoke refresh token | No |
| GET | `/auth/me` | Get current user info | Yes |
| GET | `/auth/sessions` | List user's sessions | Yes |
| GET | `/auth/sessions/{id}/history` | Get session message history | Yes |
| POST | `/auth/sessions/{id}/fork` | Fork a session | Yes |
| DELETE | `/auth/sessions/{id}` | Delete a session | Yes |

### Protected Endpoints

When auth is enabled, all API endpoints except the ones listed above require a valid Bearer token in the Authorization header:

```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

Or an API key in the X-API-Key header:

```
X-API-Key: dk_a1b2c3d4_...
```

### Token Format

Access tokens contain:

```json
{
  "sub": "user_id",
  "type": "access",
  "iss": "digitorn",
  "iat": 1710000000,
  "exp": 1710000900,
  "email": "user@example.com",
  "name": "John Doe",
  "roles": ["developer"],
  "perms": ["apps:read", "apps:write", "agents:run"]
}
```

## Security Considerations

- Passwords are hashed with bcrypt (12 rounds). Verification uses constant-time comparison.
- Refresh tokens are stored as SHA-256 hashes in the database. The raw token is never stored.
- JWT secret keys are auto-generated (64 bytes hex) and stored with 600 permissions (owner-only).
- API keys use the `dk_` prefix for easy identification in logs and key scanning tools.
- OAuth2 state parameters prevent CSRF attacks on the authorization flow.
- Session isolation is enforced at the query level. Users cannot access other users' sessions even with a valid token.
- The auth middleware runs before all request handlers. There is no way to bypass it for protected endpoints.

## Database Schema

```mermaid
erDiagram
    User ||--o{ UserRole : has
    User ||--o{ UserSession : owns
    User ||--o{ UserOAuthToken : has
    User ||--o{ RefreshToken : has
    User ||--o{ APIKey : owns
    Role ||--o{ UserRole : assigned_to

    User {
        string id PK
        string external_id
        string provider
        string email
        string display_name
        json attributes
        boolean is_active
        datetime last_seen_at
    }

    Role {
        string id PK
        string name UK
        string description
        boolean is_builtin
        json permissions
    }

    UserRole {
        string id PK
        string user_id FK
        string role_id FK
        string app_id FK
        string granted_by
    }

    RefreshToken {
        string id PK
        string user_id FK
        string token_hash UK
        datetime expires_at
        boolean revoked
    }

    APIKey {
        string id PK
        string user_id FK
        string name
        string key_hash UK
        string key_prefix
        string app_id FK
        json permissions
        boolean is_active
        datetime expires_at
    }
```
