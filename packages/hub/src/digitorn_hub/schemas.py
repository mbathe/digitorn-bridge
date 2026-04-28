from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

# ---------- Auth & users ----------

PasswordStr = Annotated[str, StringConstraints(min_length=8, max_length=120)]
DisplayNameStr = Annotated[str, StringConstraints(min_length=1, max_length=80)]


class UserRegister(BaseModel):
    email: EmailStr
    password: PasswordStr
    display_name: DisplayNameStr


class UserLogin(BaseModel):
    email: EmailStr
    password: PasswordStr


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: EmailStr
    display_name: str
    role: str
    is_active: bool
    created_at: datetime


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshIn(BaseModel):
    refresh_token: str


# ---------- API tokens ----------


class ApiTokenCreate(BaseModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    scopes: list[str] = Field(default_factory=lambda: ["packages:read"])


class ApiTokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    last_used_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None


class ApiTokenIssued(ApiTokenOut):
    plaintext: str = Field(description="Returned ONCE on creation. Store securely.")


# ---------- Publishers ----------

SLUG_REGEX = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$")


def _validate_slug(v: str) -> str:
    if not SLUG_REGEX.match(v):
        raise ValueError(
            "slug must be lowercase, 3-64 chars, [a-z0-9_-], starting/ending with [a-z0-9]"
        )
    return v


class PublisherCreate(BaseModel):
    slug: Annotated[str, StringConstraints(min_length=3, max_length=64)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    bio: str | None = None
    website: str | None = None

    @field_validator("slug")
    @classmethod
    def _slug_format(cls, v: str) -> str:
        return _validate_slug(v)


class PublisherUpdate(BaseModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = None
    bio: str | None = None
    website: str | None = None


class PublisherOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    slug: str
    name: str
    bio: str | None
    website: str | None
    verified: bool
    created_at: datetime


# ---------- Packages (used by phases 3-4) ----------


class TagOut(BaseModel):
    tag: str


class PackageSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    publisher_slug: str
    publisher_verified: bool
    package_id: str
    name: str
    description: str | None
    category: str | None
    risk_level: str
    icon_url: str | None
    latest_version: str | None
    total_downloads: int
    avg_rating: float | None
    review_count: int
    tags: list[str]
    updated_at: datetime


class PackageVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    version: str
    archive_size: int
    archive_sha256: str
    yanked: bool
    yanked_reason: str | None
    downloads: int
    released_at: datetime


class PackageDetailOut(PackageSummaryOut):
    versions: list[PackageVersionOut]
    manifest: dict


class SearchHit(PackageSummaryOut):
    score: float


# ---------- Reviews ----------


class ReviewCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    body: Annotated[str | None, StringConstraints(max_length=4000)] = None


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    package_id: uuid.UUID
    user_id: uuid.UUID
    user_display_name: str | None = None
    rating: int
    body: str | None
    hidden: bool
    created_at: datetime
    updated_at: datetime


class ReviewListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    avg_rating: float | None
    review_count: int
    distribution: dict[int, int]  # {1: 0, 2: 0, 3: 1, 4: 5, 5: 10}
    items: list[ReviewOut]


# ---------- Reports ----------

ReportReason = Annotated[
    str,
    StringConstraints(min_length=1, max_length=40, pattern=r"^(malware|spam|abuse|copyright|broken|other)$"),
]


class ReportCreate(BaseModel):
    reason: ReportReason
    details: Annotated[str | None, StringConstraints(max_length=4000)] = None


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    package_id: uuid.UUID
    publisher_slug: str | None = None
    package_id_str: str | None = None
    reporter_user_id: uuid.UUID | None
    reason: str
    details: str | None
    status: str
    created_at: datetime
    resolved_at: datetime | None
    resolution_note: str | None


class ReportResolveIn(BaseModel):
    status: Annotated[
        str,
        StringConstraints(pattern=r"^(reviewing|resolved|rejected)$"),
    ]
    note: Annotated[str | None, StringConstraints(max_length=2000)] = None


# ---------- Stats ----------


class StatsBucket(BaseModel):
    date: str  # YYYY-MM-DD
    downloads: int


class PackageStatsOut(BaseModel):
    publisher_slug: str
    package_id: str
    range_days: int
    total_downloads_in_range: int
    avg_per_day: float
    series: list[StatsBucket]
    by_version: dict[str, int]


class HubOverviewStats(BaseModel):
    total_packages: int
    total_versions: int
    total_publishers: int
    verified_publishers: int
    total_users: int
    total_downloads_all_time: int
    downloads_last_24h: int
    downloads_last_7d: int
    open_reports: int


class SearchResponse(BaseModel):
    query: str
    total: int
    page: int
    page_size: int
    hits: list[SearchHit]


# ---------- Daemon bridge ----------


class DaemonBridgeRequest(BaseModel):
    """Sent by a trusted central daemon to mint a Hub session for one
    of its users.

    The signature covers the canonical JSON of every other field in
    sorted-key order (no whitespace) - see
    `digitorn.core.api.hub_bridge.canonical_payload` for the exact
    serialisation."""

    daemon_name: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    user_id: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    email: EmailStr
    display_name: DisplayNameStr | None = None
    # Unix epoch seconds - server rejects if outside the configured
    # `daemon_bridge_max_clock_skew_seconds` window.
    ts: int
    # 16 bytes hex - single-use within the freshness window.
    nonce: Annotated[str, StringConstraints(min_length=16, max_length=64)]
    # base64-encoded ed25519 signature (64 raw bytes -> 88 chars).
    signature: Annotated[str, StringConstraints(min_length=64, max_length=128)]


class DaemonBridgeResponse(BaseModel):
    """Successful bridge response - drop-in replacement for what the
    daemon would have got from `/auth/login`."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
    # Echoed so the caller knows what got auto-provisioned vs. linked.
    created: bool = False
