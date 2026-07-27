"""Admin blog CMS — posts are stored as JSON objects in the R2 bucket, not in
the database. ``blog/{slug}.json`` is the canonical record for one post;
``blog/index.json`` is a denormalized array of every post, rebuilt on each
write, so the public site (and this list endpoint) can fetch everything in
one request instead of enumerating the bucket.
"""

import json
import re
from pathlib import PurePosixPath
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.api.admin_catalog import require_role
from app.core.config import settings

router = APIRouter(prefix="/admin/blog", tags=["admin-blog"])

_INDEX_KEY = "blog/index.json"
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _require_r2_configured() -> None:
    if not all(
        [
            settings.r2_endpoint,
            settings.r2_bucket,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
            settings.r2_public_base_url,
        ]
    ):
        raise HTTPException(status_code=503, detail="R2 is not configured")


def _r2_client():
    try:
        import boto3
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="boto3 is not installed") from exc

    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )


def _post_key(slug: str) -> str:
    return f"blog/{slug}.json"


def _get_json(client, key: str) -> dict | list | None:
    from botocore.exceptions import ClientError

    try:
        obj = client.get_object(Bucket=settings.r2_bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(obj["Body"].read())


def _put_json(client, key: str, data) -> None:
    client.put_object(
        Bucket=settings.r2_bucket,
        Key=key,
        Body=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )


def _rebuild_index(client) -> list[dict]:
    posts: list[dict] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.r2_bucket, Prefix="blog/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key == _INDEX_KEY or not key.endswith(".json"):
                continue
            post = _get_json(client, key)
            if post:
                posts.append(post)
    posts.sort(key=lambda p: p.get("publishedOn", ""), reverse=True)
    _put_json(client, _INDEX_KEY, posts)
    return posts


class BlogSectionPayload(BaseModel):
    heading: str = Field(min_length=1, max_length=200)
    body: list[str] = Field(default_factory=list)
    image_url: str | None = None


class BlogFaqPayload(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    answer: str = Field(min_length=1, max_length=2000)


class BlogPostPayload(BaseModel):
    slug: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=400)
    heroTitle: str = Field(min_length=1, max_length=200)
    heroIntro: str = Field(min_length=1, max_length=2000)
    readingTimeMinutes: int = Field(ge=1, le=60)
    publishedOn: str
    updatedOn: str | None = None
    author: str = Field(min_length=1, max_length=120)
    status: str = "published"  # "draft" | "published"
    category: str = "pillar"  # "pillar" | "cluster"
    pillarSlug: str | None = None
    sections: list[BlogSectionPayload] = Field(default_factory=list)
    faqs: list[BlogFaqPayload] = Field(default_factory=list)
    relatedGuideSlugs: list[str] = Field(default_factory=list)
    relatedPostSlugs: list[str] = Field(default_factory=list)
    seoKeywords: list[str] = Field(default_factory=list)
    image_url: str | None = None

    def to_record(self) -> dict:
        data = self.model_dump()
        data["path"] = f"/blog/{self.slug}"
        return data


def _validate_slug(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail="slug must be lowercase letters, numbers, and hyphens only",
        )


@router.get("", dependencies=[Depends(require_role("owner", "ops", "blog_editor"))])
async def list_posts() -> dict:
    _require_r2_configured()
    client = _r2_client()
    posts = _get_json(client, _INDEX_KEY)
    if posts is None:
        posts = _rebuild_index(client)
    return {"posts": posts}


@router.get("/{slug}", dependencies=[Depends(require_role("owner", "ops", "blog_editor"))])
async def get_post(slug: str) -> dict:
    _require_r2_configured()
    client = _r2_client()
    post = _get_json(client, _post_key(slug))
    if post is None:
        raise HTTPException(status_code=404, detail="post not found")
    return {"post": post}


@router.post("", dependencies=[Depends(require_role("owner", "ops", "blog_editor"))])
async def create_post(payload: BlogPostPayload) -> dict:
    _require_r2_configured()
    _validate_slug(payload.slug)
    client = _r2_client()
    if _get_json(client, _post_key(payload.slug)) is not None:
        raise HTTPException(status_code=409, detail="a post with this slug already exists")
    record = payload.to_record()
    _put_json(client, _post_key(payload.slug), record)
    _rebuild_index(client)
    return {"post": record}


@router.patch("/{slug}", dependencies=[Depends(require_role("owner", "ops", "blog_editor"))])
async def update_post(slug: str, payload: BlogPostPayload) -> dict:
    _require_r2_configured()
    _validate_slug(payload.slug)
    client = _r2_client()
    if _get_json(client, _post_key(slug)) is None:
        raise HTTPException(status_code=404, detail="post not found")
    record = payload.to_record()
    if payload.slug != slug:
        # Renaming the slug — write the new key, drop the old one.
        client.delete_object(Bucket=settings.r2_bucket, Key=_post_key(slug))
    _put_json(client, _post_key(payload.slug), record)
    _rebuild_index(client)
    return {"post": record}


@router.delete("/{slug}", dependencies=[Depends(require_role("owner", "ops", "blog_editor"))])
async def delete_post(slug: str) -> dict:
    _require_r2_configured()
    client = _r2_client()
    if _get_json(client, _post_key(slug)) is None:
        raise HTTPException(status_code=404, detail="post not found")
    client.delete_object(Bucket=settings.r2_bucket, Key=_post_key(slug))
    _rebuild_index(client)
    return {"ok": True}


@router.post("/{slug}/image", dependencies=[Depends(require_role("owner", "ops", "blog_editor"))])
async def upload_post_image(slug: str, file: UploadFile = File(...)) -> dict:
    _require_r2_configured()
    client = _r2_client()
    post = _get_json(client, _post_key(slug))
    if post is None:
        raise HTTPException(status_code=404, detail="post not found")

    suffix = PurePosixPath(file.filename or "image").suffix.lower()[:12] or ".jpg"
    key = f"blog/images/{slug}/{uuid4().hex}{suffix}"
    body = await file.read()
    client.put_object(
        Bucket=settings.r2_bucket,
        Key=key,
        Body=body,
        ContentType=file.content_type or "application/octet-stream",
    )

    public_url = f"{settings.r2_public_base_url.rstrip('/')}/{key}"
    post["image_url"] = public_url
    _put_json(client, _post_key(slug), post)
    _rebuild_index(client)
    return {"image_url": public_url, "post": post}
