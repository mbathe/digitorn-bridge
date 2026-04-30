"""HTTP API surface of digitorn-auth.

Routers:
  - auth:    /auth/login, /auth/register, /auth/refresh, /auth/logout
  - oauth:   /auth/oauth/{provider}/start, /auth/oauth/{provider}/callback
  - devices: /auth/devices/pair, /auth/devices/{id}/revalidate, DELETE /auth/devices/{id}
  - jwks:    /.well-known/openid-configuration, /.well-known/jwks.json
"""
