"""Pydantic contracts for the agency domain.

Create and read models exist for every entity so a later ticket can persist
them without inventing a second shape. Update models exist for Brand, Client, Contact, a content draft, the brand
profile, and an access row. A content or profile update cannot carry a brand id.

``brand_id`` is required on every create model except ``BrandCreate``. Brand
is the root. Unknown fields are rejected so a typo does not silently drop.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agency.models import (
    BODY_MAX,
    EMAIL_MAX,
    HANDLE_MAX,
    ID_MAX,
    ID_PATTERN,
    NAME_MAX,
    NOTES_MAX,
    PHONE_MAX,
    PROFILE_LIST_MAX,
    ROLE_MAX,
    SAMPLE_POST_MAX,
    SLUG_MAX,
    SLUG_PATTERN,
    TITLE_MAX,
    UNIT_MAX,
    URL_MAX,
    WORD_MAX,
    AccessPlatform,
    AccessStatus,
    ApprovalStatus,
    ApprovalSubject,
    BrandKind,
    CalendarSlotStatus,
    CampaignStatus,
    ChannelKind,
    ClientStatus,
    ContentIdeaStatus,
    ContentItemStatus,
    DeliverableStatus,
    EstimateStatus,
    LeadStage,
    ProjectStatus,
    TaskStatus,
    TemplateKind,
)


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _reject_null(value: str | None) -> str | None:
    """Omitted fields stay at their default. An explicit null is a mistake.

    Validators do not run for an omitted default, so this still allows PATCH
    bodies that leave the field out.
    """
    if value is None:
        raise ValueError("must not be null")
    return value


def _id_field():
    return Field(min_length=1, max_length=ID_MAX, pattern=ID_PATTERN)


def _optional_id_field():
    """An id that may be omitted or cleared. A non-empty value must match."""
    return Field(default=None, min_length=1, max_length=ID_MAX, pattern=ID_PATTERN)


class BrandCreate(APIModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    kind: BrandKind
    slug: str | None = Field(default=None, min_length=1, max_length=SLUG_MAX, pattern=SLUG_PATTERN)
    notes: str = Field(default="", max_length=NOTES_MAX)

    @field_validator("slug")
    @classmethod
    def slug_not_null(cls, value: str | None) -> str | None:
        return _reject_null(value)


class BrandUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX)
    kind: BrandKind | None = None
    slug: str | None = Field(default=None, min_length=1, max_length=SLUG_MAX, pattern=SLUG_PATTERN)
    notes: str | None = Field(default=None, max_length=NOTES_MAX)

    @field_validator("name", "slug", "notes")
    @classmethod
    def text_not_null(cls, value: str | None) -> str | None:
        return _reject_null(value)


class BrandRead(APIModel):
    id: str
    name: str
    slug: str
    kind: BrandKind
    notes: str
    created_at: datetime
    updated_at: datetime


class ClientCreate(APIModel):
    brand_id: str = _id_field()
    name: str = Field(min_length=1, max_length=NAME_MAX)
    status: ClientStatus = ClientStatus.ACTIVE
    client_brand_id: str | None = _optional_id_field()
    website: str = Field(default="", max_length=URL_MAX)
    notes: str = Field(default="", max_length=NOTES_MAX)


class ClientUpdate(APIModel):
    brand_id: str | None = _optional_id_field()
    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX)
    status: ClientStatus | None = None
    client_brand_id: str | None = _optional_id_field()
    website: str | None = Field(default=None, max_length=URL_MAX)
    notes: str | None = Field(default=None, max_length=NOTES_MAX)

    @field_validator("brand_id", "name", "website", "notes")
    @classmethod
    def text_not_null(cls, value: str | None) -> str | None:
        return _reject_null(value)


class ClientRead(APIModel):
    id: str
    brand_id: str
    name: str
    status: ClientStatus
    client_brand_id: str | None
    website: str
    notes: str
    created_at: datetime
    updated_at: datetime


class ContactCreate(APIModel):
    brand_id: str = _id_field()
    name: str = Field(min_length=1, max_length=NAME_MAX)
    client_id: str | None = _optional_id_field()
    email: str = Field(default="", max_length=EMAIL_MAX)
    phone: str = Field(default="", max_length=PHONE_MAX)
    role: str = Field(default="", max_length=ROLE_MAX)
    notes: str = Field(default="", max_length=NOTES_MAX)


class ContactUpdate(APIModel):
    brand_id: str | None = _optional_id_field()
    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX)
    client_id: str | None = _optional_id_field()
    email: str | None = Field(default=None, max_length=EMAIL_MAX)
    phone: str | None = Field(default=None, max_length=PHONE_MAX)
    role: str | None = Field(default=None, max_length=ROLE_MAX)
    notes: str | None = Field(default=None, max_length=NOTES_MAX)

    @field_validator("brand_id", "name", "email", "phone", "role", "notes")
    @classmethod
    def text_not_null(cls, value: str | None) -> str | None:
        return _reject_null(value)


class ContactRead(APIModel):
    id: str
    brand_id: str
    name: str
    client_id: str | None
    email: str
    phone: str
    role: str
    notes: str
    created_at: datetime
    updated_at: datetime


class LeadCreate(APIModel):
    brand_id: str = _id_field()
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    stage: LeadStage = LeadStage.NEW
    client_id: str | None = _optional_id_field()
    contact_id: str | None = _optional_id_field()
    source: str = Field(default="", max_length=120)
    value_cents: int | None = Field(default=None, ge=0)
    notes: str = Field(default="", max_length=NOTES_MAX)


class LeadRead(APIModel):
    id: str
    brand_id: str
    title: str
    stage: LeadStage
    client_id: str | None
    contact_id: str | None
    source: str
    value_cents: int | None
    notes: str
    created_at: datetime
    updated_at: datetime


class LeadStageHistoryCreate(APIModel):
    brand_id: str = _id_field()
    lead_id: str = _id_field()
    to_stage: LeadStage
    from_stage: str = Field(default="", max_length=32)
    note: str = Field(default="", max_length=NOTES_MAX)
    changed_at: datetime | None = None


class LeadStageHistoryRead(APIModel):
    id: str
    brand_id: str
    lead_id: str
    to_stage: LeadStage
    from_stage: str
    note: str
    changed_at: datetime


class ProjectCreate(APIModel):
    brand_id: str = _id_field()
    name: str = Field(min_length=1, max_length=NAME_MAX)
    status: ProjectStatus = ProjectStatus.DRAFT
    client_id: str | None = _optional_id_field()
    description: str = Field(default="", max_length=NOTES_MAX)
    starts_on: date | None = None
    ends_on: date | None = None


class ProjectRead(APIModel):
    id: str
    brand_id: str
    name: str
    status: ProjectStatus
    client_id: str | None
    description: str
    starts_on: date | None
    ends_on: date | None
    created_at: datetime
    updated_at: datetime


class TaskCreate(APIModel):
    brand_id: str = _id_field()
    project_id: str = _id_field()
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    status: TaskStatus = TaskStatus.TODO
    assignee: str = Field(default="", max_length=NAME_MAX)
    due_on: date | None = None
    notes: str = Field(default="", max_length=NOTES_MAX)
    sort_order: int = Field(default=0, ge=0)


class TaskRead(APIModel):
    id: str
    brand_id: str
    project_id: str
    title: str
    status: TaskStatus
    assignee: str
    due_on: date | None
    notes: str
    sort_order: int
    created_at: datetime
    updated_at: datetime


class DeliverableCreate(APIModel):
    brand_id: str = _id_field()
    project_id: str = _id_field()
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    status: DeliverableStatus = DeliverableStatus.PENDING
    task_id: str | None = _optional_id_field()
    due_on: date | None = None
    notes: str = Field(default="", max_length=NOTES_MAX)


class DeliverableRead(APIModel):
    id: str
    brand_id: str
    project_id: str
    title: str
    status: DeliverableStatus
    task_id: str | None
    due_on: date | None
    notes: str
    created_at: datetime
    updated_at: datetime


class ChannelCreate(APIModel):
    brand_id: str = _id_field()
    name: str = Field(min_length=1, max_length=NAME_MAX)
    kind: ChannelKind
    handle: str = Field(default="", max_length=HANDLE_MAX)
    notes: str = Field(default="", max_length=NOTES_MAX)


class ChannelRead(APIModel):
    id: str
    brand_id: str
    name: str
    kind: ChannelKind
    handle: str
    notes: str
    created_at: datetime
    updated_at: datetime


class TemplateCreate(APIModel):
    brand_id: str = _id_field()
    name: str = Field(min_length=1, max_length=NAME_MAX)
    kind: TemplateKind = TemplateKind.OTHER
    body: str = Field(default="", max_length=BODY_MAX)


class TemplateRead(APIModel):
    id: str
    brand_id: str
    name: str
    kind: TemplateKind
    body: str
    created_at: datetime
    updated_at: datetime


class CampaignCreate(APIModel):
    brand_id: str = _id_field()
    name: str = Field(min_length=1, max_length=NAME_MAX)
    status: CampaignStatus = CampaignStatus.DRAFT
    objective: str = Field(default="", max_length=NOTES_MAX)
    starts_on: date | None = None
    ends_on: date | None = None
    notes: str = Field(default="", max_length=NOTES_MAX)


class CampaignRead(APIModel):
    id: str
    brand_id: str
    name: str
    status: CampaignStatus
    objective: str
    starts_on: date | None
    ends_on: date | None
    notes: str
    created_at: datetime
    updated_at: datetime


class ContentIdeaCreate(APIModel):
    brand_id: str = _id_field()
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    body: str = Field(default="", max_length=BODY_MAX)
    status: ContentIdeaStatus = ContentIdeaStatus.CAPTURED
    source: str = Field(default="", max_length=120)


class ContentIdeaRead(APIModel):
    id: str
    brand_id: str
    title: str
    body: str
    status: ContentIdeaStatus
    source: str
    created_at: datetime
    updated_at: datetime


class ContentItemCreate(APIModel):
    brand_id: str = _id_field()
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    idea_id: str | None = _optional_id_field()
    channel_id: str | None = _optional_id_field()
    campaign_id: str | None = _optional_id_field()
    body: str = Field(default="", max_length=BODY_MAX)
    status: ContentItemStatus = ContentItemStatus.DRAFT
    scheduled_at: datetime | None = None


class ContentItemRead(APIModel):
    id: str
    brand_id: str
    title: str
    idea_id: str | None
    channel_id: str | None
    campaign_id: str | None
    body: str
    status: ContentItemStatus
    scheduled_at: datetime | None
    source_item_id: str | None = None
    created_at: datetime
    updated_at: datetime


class ContentItemUpdate(APIModel):
    """Fields a direct edit may change. Brand, channel and source are not among them."""

    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    body: str | None = Field(default=None, max_length=BODY_MAX)
    status: ContentItemStatus | None = None

    @field_validator("title", "body")
    @classmethod
    def text_not_null(cls, value: str | None) -> str | None:
        return _reject_null(value)


class DuplicateContentRequest(APIModel):
    target_brand_id: str = _id_field()


class ScheduleContentRequest(APIModel):
    """Local schedule. Applying it does not publish the item."""

    channel_id: str = _id_field()
    scheduled_at: datetime


class ContentRevisionCreate(APIModel):
    brand_id: str = _id_field()
    content_item_id: str = _id_field()
    revision_no: int = Field(ge=1)
    body: str = Field(default="", max_length=BODY_MAX)
    note: str = Field(default="", max_length=NOTES_MAX)


class ContentRevisionRead(APIModel):
    id: str
    brand_id: str
    content_item_id: str
    revision_no: int
    body: str
    note: str
    created_at: datetime


class CalendarSlotCreate(APIModel):
    brand_id: str = _id_field()
    starts_at: datetime
    channel_id: str | None = _optional_id_field()
    content_item_id: str | None = _optional_id_field()
    title: str = Field(default="", max_length=TITLE_MAX)
    status: CalendarSlotStatus = CalendarSlotStatus.OPEN
    ends_at: datetime | None = None


class CalendarSlotRead(APIModel):
    id: str
    brand_id: str
    starts_at: datetime
    channel_id: str | None
    content_item_id: str | None
    title: str
    status: CalendarSlotStatus
    ends_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ApprovalCreate(APIModel):
    brand_id: str = _id_field()
    subject_type: ApprovalSubject
    subject_id: str = _id_field()
    status: ApprovalStatus = ApprovalStatus.PENDING
    note: str = Field(default="", max_length=NOTES_MAX)


class ApprovalRead(APIModel):
    id: str
    brand_id: str
    subject_type: ApprovalSubject
    subject_id: str
    status: ApprovalStatus
    note: str
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime


class KPIDefinitionCreate(APIModel):
    brand_id: str = _id_field()
    name: str = Field(min_length=1, max_length=NAME_MAX)
    campaign_id: str | None = _optional_id_field()
    unit: str = Field(default="", max_length=UNIT_MAX)
    target_value: float | None = None


class KPIDefinitionRead(APIModel):
    id: str
    brand_id: str
    name: str
    campaign_id: str | None
    unit: str
    target_value: float | None
    created_at: datetime
    updated_at: datetime


class MetricEntryCreate(APIModel):
    brand_id: str = _id_field()
    kpi_id: str = _id_field()
    value: float
    recorded_on: date
    campaign_id: str | None = _optional_id_field()
    note: str = Field(default="", max_length=NOTES_MAX)


class MetricEntryRead(APIModel):
    id: str
    brand_id: str
    kpi_id: str
    value: float
    recorded_on: date
    campaign_id: str | None
    note: str
    created_at: datetime


class CompetitorCreate(APIModel):
    brand_id: str = _id_field()
    name: str = Field(min_length=1, max_length=NAME_MAX)
    website: str = Field(default="", max_length=URL_MAX)
    notes: str = Field(default="", max_length=NOTES_MAX)


class CompetitorRead(APIModel):
    id: str
    brand_id: str
    name: str
    website: str
    notes: str
    created_at: datetime
    updated_at: datetime


class ResearchNoteCreate(APIModel):
    brand_id: str = _id_field()
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    competitor_id: str | None = _optional_id_field()
    body: str = Field(default="", max_length=BODY_MAX)
    source_url: str = Field(default="", max_length=URL_MAX)


class ResearchNoteRead(APIModel):
    id: str
    brand_id: str
    title: str
    competitor_id: str | None
    body: str
    source_url: str
    created_at: datetime
    updated_at: datetime


class ServiceTemplateCreate(APIModel):
    brand_id: str = _id_field()
    name: str = Field(min_length=1, max_length=NAME_MAX)
    description: str = Field(default="", max_length=NOTES_MAX)
    unit: str = Field(default="item", min_length=1, max_length=UNIT_MAX)
    default_unit_price_cents: int = Field(default=0, ge=0)


class ServiceTemplateRead(APIModel):
    id: str
    brand_id: str
    name: str
    description: str
    unit: str
    default_unit_price_cents: int
    created_at: datetime
    updated_at: datetime


class EstimateCreate(APIModel):
    brand_id: str = _id_field()
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    status: EstimateStatus = EstimateStatus.DRAFT
    client_id: str | None = _optional_id_field()
    project_id: str | None = _optional_id_field()
    currency: str = Field(default="INR", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    notes: str = Field(default="", max_length=NOTES_MAX)


class EstimateRead(APIModel):
    id: str
    brand_id: str
    title: str
    status: EstimateStatus
    client_id: str | None
    project_id: str | None
    currency: str
    notes: str
    created_at: datetime
    updated_at: datetime


class EstimateLineCreate(APIModel):
    brand_id: str = _id_field()
    estimate_id: str = _id_field()
    description: str = Field(min_length=1, max_length=TITLE_MAX)
    quantity: float = Field(default=1, gt=0, le=1_000_000)
    unit_price_cents: int = Field(default=0, ge=0)
    service_template_id: str | None = _optional_id_field()
    sort_order: int = Field(default=0, ge=0)


class ProfileCompetitorIn(APIModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    website: str = Field(default="", max_length=URL_MAX)
    notes: str = Field(default="", max_length=NOTES_MAX)


class ProfileChannelIn(APIModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    kind: ChannelKind


class ProfileApprovalContactIn(APIModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    email: str = Field(default="", max_length=EMAIL_MAX)
    role: str = Field(default="", max_length=ROLE_MAX)


class ProfileServiceIn(APIModel):
    """A service the client already bought. Naming it does not create an order."""

    name: str = Field(min_length=1, max_length=NAME_MAX)
    notes: str = Field(default="", max_length=NOTES_MAX)


class BrandProfileCreate(APIModel):
    brand_id: str = _id_field()
    goals: str = Field(default="", max_length=NOTES_MAX)
    audience: str = Field(default="", max_length=NOTES_MAX)
    positioning: str = Field(default="", max_length=NOTES_MAX)
    voice_description: str = Field(default="", max_length=NOTES_MAX)
    words_to_use: list[str] = Field(default_factory=list, max_length=PROFILE_LIST_MAX)
    words_to_avoid: list[str] = Field(default_factory=list, max_length=PROFILE_LIST_MAX)
    sample_posts: list[str] = Field(default_factory=list, max_length=PROFILE_LIST_MAX)
    competitors: list[ProfileCompetitorIn] = Field(default_factory=list, max_length=PROFILE_LIST_MAX)
    active_channels: list[ProfileChannelIn] = Field(default_factory=list, max_length=PROFILE_LIST_MAX)
    approval_contacts: list[ProfileApprovalContactIn] = Field(
        default_factory=list, max_length=PROFILE_LIST_MAX
    )
    services_bought: list[ProfileServiceIn] = Field(default_factory=list, max_length=PROFILE_LIST_MAX)

    @field_validator("words_to_use", "words_to_avoid")
    @classmethod
    def words_fit(cls, value: list[str]) -> list[str]:
        for item in value:
            if len(item) > WORD_MAX:
                raise ValueError(f"must be at most {WORD_MAX} characters")
        return value

    @field_validator("sample_posts")
    @classmethod
    def samples_fit(cls, value: list[str]) -> list[str]:
        for item in value:
            if len(item) > SAMPLE_POST_MAX:
                raise ValueError(f"must be at most {SAMPLE_POST_MAX} characters")
        return value


class BrandProfileUpdate(APIModel):
    goals: str | None = Field(default=None, max_length=NOTES_MAX)
    audience: str | None = Field(default=None, max_length=NOTES_MAX)
    positioning: str | None = Field(default=None, max_length=NOTES_MAX)
    voice_description: str | None = Field(default=None, max_length=NOTES_MAX)
    words_to_use: list[str] | None = Field(default=None, max_length=PROFILE_LIST_MAX)
    words_to_avoid: list[str] | None = Field(default=None, max_length=PROFILE_LIST_MAX)
    sample_posts: list[str] | None = Field(default=None, max_length=PROFILE_LIST_MAX)
    competitors: list[ProfileCompetitorIn] | None = Field(default=None, max_length=PROFILE_LIST_MAX)
    active_channels: list[ProfileChannelIn] | None = Field(default=None, max_length=PROFILE_LIST_MAX)
    approval_contacts: list[ProfileApprovalContactIn] | None = Field(
        default=None, max_length=PROFILE_LIST_MAX
    )
    services_bought: list[ProfileServiceIn] | None = Field(default=None, max_length=PROFILE_LIST_MAX)

    @field_validator(
        "goals",
        "audience",
        "positioning",
        "voice_description",
        "words_to_use",
        "words_to_avoid",
        "sample_posts",
        "competitors",
        "active_channels",
        "approval_contacts",
        "services_bought",
    )
    @classmethod
    def present(cls, value: Any) -> Any:
        return _reject_null(value)

    @field_validator("words_to_use", "words_to_avoid")
    @classmethod
    def words_fit(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        for item in value:
            if len(item) > WORD_MAX:
                raise ValueError(f"must be at most {WORD_MAX} characters")
        return value

    @field_validator("sample_posts")
    @classmethod
    def samples_fit(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        for item in value:
            if len(item) > SAMPLE_POST_MAX:
                raise ValueError(f"must be at most {SAMPLE_POST_MAX} characters")
        return value


class BrandProfileRead(APIModel):
    id: str
    brand_id: str
    goals: str
    audience: str
    positioning: str
    voice_description: str
    words_to_use: list[str]
    words_to_avoid: list[str]
    sample_posts: list[str]
    competitors: list[dict[str, str]]
    active_channels: list[dict[str, str]]
    approval_contacts: list[dict[str, str]]
    services_bought: list[dict[str, str]]
    created_at: datetime
    updated_at: datetime


class AccessItemCreate(APIModel):
    brand_id: str = _id_field()
    platform: AccessPlatform
    status: AccessStatus = AccessStatus.REQUESTED
    notes: str = Field(default="", max_length=NOTES_MAX)


class AccessItemUpdate(APIModel):
    status: AccessStatus | None = None
    notes: str | None = Field(default=None, max_length=NOTES_MAX)

    @field_validator("status", "notes")
    @classmethod
    def present(cls, value: Any) -> Any:
        return _reject_null(value)


class AccessItemRead(APIModel):
    id: str
    brand_id: str
    platform: AccessPlatform
    status: AccessStatus
    notes: str
    created_at: datetime
    updated_at: datetime


class EstimateLineRead(APIModel):
    id: str
    brand_id: str
    estimate_id: str
    description: str
    quantity: float
    unit_price_cents: int
    service_template_id: str | None
    sort_order: int
    created_at: datetime
    updated_at: datetime


# Create models that are not the brand root. Each one must demand brand_id.
BRAND_SCOPED_CREATE_MODELS: tuple[type[APIModel], ...] = (
    ClientCreate,
    ContactCreate,
    LeadCreate,
    LeadStageHistoryCreate,
    ProjectCreate,
    TaskCreate,
    DeliverableCreate,
    ChannelCreate,
    TemplateCreate,
    CampaignCreate,
    ContentIdeaCreate,
    ContentItemCreate,
    ContentRevisionCreate,
    CalendarSlotCreate,
    ApprovalCreate,
    KPIDefinitionCreate,
    MetricEntryCreate,
    CompetitorCreate,
    ResearchNoteCreate,
    ServiceTemplateCreate,
    EstimateCreate,
    EstimateLineCreate,
    BrandProfileCreate,
    AccessItemCreate,
)
