"""The channel-template catalog inserted by migration 0005.

Limits that a platform actually enforces are called out below. The rest are
planning caps so every field has a number the validator can refuse. Nothing
here is pushed to a network.

- Instagram caption: 2200
- YouTube title: 100. YouTube description: 5000
- LinkedIn post: 3000
- Facebook post: 63206
- SEO meta title: 60. SEO meta description: 160
- Google Ads keyword: 80. RSA headline: 30 (3 to 15). RSA description: 90 (2 to 4)
- Instagram carousel: 2 to 10 slides
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.db import execute


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    max_chars: int
    min_count: int = 1
    max_count: int = 1


@dataclass(frozen=True)
class TemplateSpec:
    id: str
    platform: str
    slug: str
    name: str
    fields: tuple[FieldSpec, ...]


def _field(
    key: str,
    label: str,
    max_chars: int,
    min_count: int = 1,
    max_count: int = 1,
) -> FieldSpec:
    return FieldSpec(key, label, max_chars, min_count, max_count)


TEMPLATES: tuple[TemplateSpec, ...] = (
    TemplateSpec(
        "tpl_ig_reel",
        "instagram",
        "reel_script",
        "Reel script",
        (_field("script", "Script", 2200),),
    ),
    TemplateSpec(
        "tpl_ig_caption",
        "instagram",
        "caption",
        "Caption",
        (_field("caption", "Caption", 2200),),
    ),
    TemplateSpec(
        "tpl_ig_carousel",
        "instagram",
        "carousel_outline",
        "Carousel outline",
        (
            _field("slide", "Slide", 2200, min_count=2, max_count=10),
            _field("caption", "Caption", 2200, min_count=0, max_count=1),
        ),
    ),
    TemplateSpec(
        "tpl_ig_story",
        "instagram",
        "story_sequence",
        "Story sequence",
        (_field("frame", "Frame", 80, min_count=1, max_count=15),),
    ),
    TemplateSpec(
        "tpl_ig_brief",
        "instagram",
        "creative_brief",
        "Creative brief",
        (_field("brief", "Brief", 5000),),
    ),
    TemplateSpec(
        "tpl_yt_long",
        "youtube",
        "long_script",
        "Long script",
        (_field("script", "Script", 20_000),),
    ),
    TemplateSpec(
        "tpl_yt_short",
        "youtube",
        "short_script",
        "Short script",
        (_field("script", "Script", 1000),),
    ),
    TemplateSpec(
        "tpl_yt_titles",
        "youtube",
        "title_set",
        "Title set",
        (_field("title", "Title", 100, min_count=1, max_count=5),),
    ),
    TemplateSpec(
        "tpl_yt_description",
        "youtube",
        "description",
        "Description",
        (_field("description", "Description", 5000),),
    ),
    TemplateSpec(
        "tpl_yt_thumb",
        "youtube",
        "thumbnail_brief",
        "Thumbnail brief",
        (_field("brief", "Brief", 500),),
    ),
    TemplateSpec(
        "tpl_li_post",
        "linkedin",
        "post",
        "Post",
        (_field("post", "Post", 3000),),
    ),
    TemplateSpec(
        "tpl_li_carousel",
        "linkedin",
        "carousel",
        "Carousel",
        (_field("slide", "Slide", 3000, min_count=2, max_count=20),),
    ),
    TemplateSpec(
        "tpl_li_case",
        "linkedin",
        "case_study",
        "Case study",
        (_field("body", "Body", 3000),),
    ),
    TemplateSpec(
        "tpl_li_update",
        "linkedin",
        "company_update",
        "Company update",
        (_field("post", "Post", 3000),),
    ),
    TemplateSpec(
        "tpl_fb_post",
        "facebook",
        "post",
        "Post",
        (_field("post", "Post", 63206),),
    ),
    TemplateSpec(
        "tpl_fb_reel",
        "facebook",
        "reel",
        "Reel",
        (_field("script", "Script", 2200),),
    ),
    TemplateSpec(
        "tpl_fb_promo",
        "facebook",
        "promo_copy",
        "Promo copy",
        (_field("copy", "Copy", 125),),
    ),
    TemplateSpec(
        "tpl_fb_community",
        "facebook",
        "community_update",
        "Community update",
        (_field("post", "Post", 5000),),
    ),
    TemplateSpec(
        "tpl_web_outline",
        "website",
        "blog_outline",
        "Blog outline",
        (_field("outline", "Outline", 20_000),),
    ),
    TemplateSpec(
        "tpl_web_page",
        "website",
        "page_brief",
        "Page brief",
        (_field("brief", "Brief", 8000),),
    ),
    TemplateSpec(
        "tpl_web_meta",
        "website",
        "meta",
        "Meta title/description",
        (
            _field("title", "Meta title", 60),
            _field("description", "Meta description", 160),
        ),
    ),
    TemplateSpec(
        "tpl_web_links",
        "website",
        "internal_links",
        "Internal links",
        (_field("entry", "Link", 300, min_count=1, max_count=30),),
    ),
    TemplateSpec(
        "tpl_gads_plan",
        "google_ads",
        "ad_group_plan",
        "Ad group plan",
        (_field("plan", "Plan", 5000),),
    ),
    TemplateSpec(
        "tpl_gads_keywords",
        "google_ads",
        "keyword_themes",
        "Keyword themes",
        (_field("theme", "Theme", 80, min_count=1, max_count=50),),
    ),
    TemplateSpec(
        "tpl_gads_rsa",
        "google_ads",
        "rsa",
        "RSA",
        (
            _field("headline", "Headline", 30, min_count=3, max_count=15),
            _field("description", "Description", 90, min_count=2, max_count=4),
        ),
    ),
)


def seed_channel_templates(conn) -> None:
    """Insert the catalog. The migration calls this once, inside its transaction."""
    for index, spec in enumerate(TEMPLATES):
        execute(
            conn,
            "INSERT INTO agy_channel_templates (id, platform, slug, name, sort_order)"
            " VALUES (?, ?, ?, ?, ?)",
            (spec.id, spec.platform, spec.slug, spec.name, index),
        )
        for field_index, field in enumerate(spec.fields):
            execute(
                conn,
                "INSERT INTO agy_template_fields ("
                " id, template_id, field_key, label, max_chars, min_count, max_count, sort_order"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{spec.id}_{field.key}",
                    spec.id,
                    field.key,
                    field.label,
                    field.max_chars,
                    field.min_count,
                    field.max_count,
                    field_index,
                ),
            )
