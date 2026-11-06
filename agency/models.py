"""Agency domain model.

Brand is the root. Every other record carries ``brand_id`` and is meaningless
without it: a content item, a project and a campaign are always work for one
brand, never a global pool. ``Brand`` itself has no ``brand_id``.

A ``Client`` is the commercial relationship, owned by a personal or agency
brand. A brand of kind ``client`` is that customer's own workspace (the place
its projects and content hang off). ``Client.client_brand_id`` links the two
when both exist. They are not the same row.

Nothing here places an order, publishes a post, or talks to an ad network.
Statuses are labels the owner sets.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, fields
from dataclasses import field as dc_field
from datetime import date, datetime
from enum import Enum
from typing import Any

# Lengths shared with the Pydantic schemas so the HTTP layer and the store
# cannot drift apart.
NAME_MAX = 200
PROFILE_LIST_MAX = 30
WORD_MAX = 80
SAMPLE_POST_MAX = 4000
SLUG_MAX = 80
NOTES_MAX = 4000
TITLE_MAX = 300
BODY_MAX = 20_000
URL_MAX = 2000
EMAIL_MAX = 320
PHONE_MAX = 40
ROLE_MAX = 120
HANDLE_MAX = 120
UNIT_MAX = 40
ID_MAX = 40
SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
ID_PATTERN = r"^[a-z0-9_]{1,40}$"

# Tables whose rows must name a brand. Kept next to the classes so a new
# entity cannot land in the migration and forget this list — the migration
# test compares it to the live catalog.
BRAND_SCOPED_TABLES: tuple[str, ...] = (
    "agy_clients",
    "agy_contacts",
    "agy_leads",
    "agy_lead_stage_history",
    "agy_projects",
    "agy_tasks",
    "agy_deliverables",
    "agy_channels",
    "agy_templates",
    "agy_campaigns",
    "agy_content_ideas",
    "agy_content_items",
    "agy_content_revisions",
    "agy_calendar_slots",
    "agy_approvals",
    "agy_kpi_definitions",
    "agy_metric_entries",
    "agy_competitors",
    "agy_research_notes",
    "agy_service_templates",
    "agy_estimates",
    "agy_estimate_lines",
    "agy_brand_profiles",
    "agy_access_items",
)

# The invariant called out by name: content, project and campaign records.
CONTENT_TABLES: tuple[str, ...] = (
    "agy_content_ideas",
    "agy_content_items",
    "agy_content_revisions",
    "agy_calendar_slots",
    "agy_channels",
    "agy_templates",
)
PROJECT_TABLES: tuple[str, ...] = (
    "agy_projects",
    "agy_tasks",
    "agy_deliverables",
)
CAMPAIGN_TABLES: tuple[str, ...] = (
    "agy_campaigns",
    "agy_kpi_definitions",
    "agy_metric_entries",
)


class AgencyError(Exception):
    """Invalid input. The API maps this to 400."""


class AgencyConflict(AgencyError):
    """Uniqueness or foreign-key refusal. The API maps this to 409."""


class BrandKind(str, Enum):
    PERSONAL = "personal"
    AGENCY = "agency"
    CLIENT = "client"


class ClientStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class LeadStage(str, Enum):
    NEW = "new"
    QUALIFIED = "qualified"
    PROPOSAL = "proposal"
    WON = "won"
    LOST = "lost"


class ProjectStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    TODO = "todo"
    DOING = "doing"
    BLOCKED = "blocked"
    DONE = "done"


class DeliverableStatus(str, Enum):
    PENDING = "pending"
    IN_REVIEW = "in_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ChannelKind(str, Enum):
    BLOG = "blog"
    NEWSLETTER = "newsletter"
    LINKEDIN = "linkedin"
    INSTAGRAM = "instagram"
    X = "x"
    YOUTUBE = "youtube"
    PODCAST = "podcast"
    OTHER = "other"


class TemplateKind(str, Enum):
    CAPTION = "caption"
    EMAIL = "email"
    BRIEF = "brief"
    PROPOSAL = "proposal"
    OTHER = "other"


class ContentIdeaStatus(str, Enum):
    CAPTURED = "captured"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PRODUCED = "produced"


class ContentItemStatus(str, Enum):
    """Where a draft sits. None of these values publishes it anywhere."""

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    ARCHIVED = "archived"


class CalendarSlotStatus(str, Enum):
    OPEN = "open"
    HELD = "held"
    FILLED = "filled"
    CANCELLED = "cancelled"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalSubject(str, Enum):
    CONTENT_ITEM = "content_item"
    CONTENT_REVISION = "content_revision"
    CAMPAIGN = "campaign"
    DELIVERABLE = "deliverable"
    ESTIMATE = "estimate"


class CampaignStatus(str, Enum):
    """Planning state. This is not an ad-account switch."""

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class EstimateStatus(str, Enum):
    """Quote state. ``sent`` means the owner marked it sent. Nothing is emailed."""

    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


class AccessPlatform(str, Enum):
    """A platform the checklist can name. The row stores a status, never a login."""

    LINKEDIN = "linkedin"
    INSTAGRAM = "instagram"
    X = "x"
    YOUTUBE = "youtube"
    FACEBOOK = "facebook"
    TIKTOK = "tiktok"
    BLOG = "blog"
    NEWSLETTER = "newsletter"
    GOOGLE_ANALYTICS = "google_analytics"
    META_ADS = "meta_ads"
    GOOGLE_ADS = "google_ads"
    WEBSITE = "website"
    EMAIL = "email"
    OTHER = "other"


class AccessStatus(str, Enum):
    """Whether access was asked for. ``granted`` is a label, not a credential."""

    REQUESTED = "requested"
    GRANTED = "granted"
    NOT_NEEDED = "not_needed"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Model:
    """JSON-safe dump. Enums and dates become strings; ``None`` stays ``None``."""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, Enum):
                value = value.value
            elif isinstance(value, date):
                # datetime is a date. isoformat keeps the time when there is one.
                value = value.isoformat()
            payload[field.name] = value
        return payload


@dataclass
class Brand(Model):
    id: str
    name: str
    slug: str
    kind: BrandKind
    notes: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Client(Model):
    id: str
    brand_id: str
    name: str
    status: ClientStatus = ClientStatus.ACTIVE
    client_brand_id: str | None = None
    website: str = ""
    notes: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Contact(Model):
    id: str
    brand_id: str
    name: str
    client_id: str | None = None
    email: str = ""
    phone: str = ""
    role: str = ""
    notes: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Lead(Model):
    id: str
    brand_id: str
    title: str
    stage: LeadStage = LeadStage.NEW
    client_id: str | None = None
    contact_id: str | None = None
    source: str = ""
    value_cents: int | None = None
    notes: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class LeadStageHistory(Model):
    id: str
    brand_id: str
    lead_id: str
    to_stage: LeadStage
    from_stage: str = ""
    note: str = ""
    changed_at: datetime | None = None


@dataclass
class Project(Model):
    id: str
    brand_id: str
    name: str
    status: ProjectStatus = ProjectStatus.DRAFT
    client_id: str | None = None
    description: str = ""
    starts_on: date | None = None
    ends_on: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Task(Model):
    id: str
    brand_id: str
    project_id: str
    title: str
    status: TaskStatus = TaskStatus.TODO
    assignee: str = ""
    due_on: date | None = None
    notes: str = ""
    sort_order: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Deliverable(Model):
    id: str
    brand_id: str
    project_id: str
    title: str
    status: DeliverableStatus = DeliverableStatus.PENDING
    task_id: str | None = None
    due_on: date | None = None
    notes: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Channel(Model):
    id: str
    brand_id: str
    name: str
    kind: ChannelKind
    handle: str = ""
    notes: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Template(Model):
    id: str
    brand_id: str
    name: str
    kind: TemplateKind = TemplateKind.OTHER
    body: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Campaign(Model):
    id: str
    brand_id: str
    name: str
    status: CampaignStatus = CampaignStatus.DRAFT
    objective: str = ""
    starts_on: date | None = None
    ends_on: date | None = None
    notes: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class ContentIdea(Model):
    id: str
    brand_id: str
    title: str
    body: str = ""
    status: ContentIdeaStatus = ContentIdeaStatus.CAPTURED
    source: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class ContentItem(Model):
    id: str
    brand_id: str
    title: str
    idea_id: str | None = None
    channel_id: str | None = None
    campaign_id: str | None = None
    body: str = ""
    status: ContentItemStatus = ContentItemStatus.DRAFT
    scheduled_at: datetime | None = None
    source_item_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class ContentRevision(Model):
    id: str
    brand_id: str
    content_item_id: str
    revision_no: int
    body: str = ""
    note: str = ""
    created_at: datetime | None = None


@dataclass
class CalendarSlot(Model):
    id: str
    brand_id: str
    starts_at: datetime
    channel_id: str | None = None
    content_item_id: str | None = None
    title: str = ""
    status: CalendarSlotStatus = CalendarSlotStatus.OPEN
    ends_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Approval(Model):
    """A decision recorded against another agency row.

    ``subject_id`` is not a foreign key on purpose: one approval table covers
    content, campaigns, deliverables and estimates without a cascade that
    could delete the decision when the subject goes away.
    """

    id: str
    brand_id: str
    subject_type: ApprovalSubject
    subject_id: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    note: str = ""
    decided_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class KPIDefinition(Model):
    id: str
    brand_id: str
    name: str
    campaign_id: str | None = None
    unit: str = ""
    target_value: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class MetricEntry(Model):
    id: str
    brand_id: str
    kpi_id: str
    value: float
    recorded_on: date
    campaign_id: str | None = None
    note: str = ""
    created_at: datetime | None = None


@dataclass
class Competitor(Model):
    id: str
    brand_id: str
    name: str
    website: str = ""
    notes: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class ResearchNote(Model):
    id: str
    brand_id: str
    title: str
    competitor_id: str | None = None
    body: str = ""
    source_url: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class ServiceTemplate(Model):
    """A rate-card line. ``default_unit_price_cents`` is a quote input, not a charge."""

    id: str
    brand_id: str
    name: str
    description: str = ""
    unit: str = "item"
    default_unit_price_cents: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Estimate(Model):
    """A quote. Accepting it does not create an order or move money."""

    id: str
    brand_id: str
    title: str
    status: EstimateStatus = EstimateStatus.DRAFT
    client_id: str | None = None
    project_id: str | None = None
    currency: str = "INR"
    notes: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class BrandProfile(Model):
    """Onboarding profile for one brand. Lists are JSON on the row.

    ``services_bought`` names work the client already bought. It is not an
    order and it does not charge anything.
    """

    id: str
    brand_id: str
    goals: str = ""
    audience: str = ""
    positioning: str = ""
    voice_description: str = ""
    words_to_use: list[str] = dc_field(default_factory=list)
    words_to_avoid: list[str] = dc_field(default_factory=list)
    sample_posts: list[str] = dc_field(default_factory=list)
    competitors: list[dict[str, str]] = dc_field(default_factory=list)
    active_channels: list[dict[str, str]] = dc_field(default_factory=list)
    approval_contacts: list[dict[str, str]] = dc_field(default_factory=list)
    services_bought: list[dict[str, str]] = dc_field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class AccessItem(Model):
    """One platform on the access checklist. Status and notes only."""

    id: str
    brand_id: str
    platform: AccessPlatform
    status: AccessStatus = AccessStatus.REQUESTED
    notes: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class EstimateLine(Model):
    id: str
    brand_id: str
    estimate_id: str
    description: str
    quantity: float = 1
    unit_price_cents: int = 0
    service_template_id: str | None = None
    sort_order: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


# Dataclasses that must expose brand_id. Brand is the root and is absent.
BRAND_SCOPED_MODELS: tuple[type[Model], ...] = (
    Client,
    Contact,
    Lead,
    LeadStageHistory,
    Project,
    Task,
    Deliverable,
    Channel,
    Template,
    Campaign,
    ContentIdea,
    ContentItem,
    ContentRevision,
    CalendarSlot,
    Approval,
    KPIDefinition,
    MetricEntry,
    Competitor,
    ResearchNote,
    ServiceTemplate,
    Estimate,
    EstimateLine,
    BrandProfile,
    AccessItem,
)
