#!/usr/bin/env python3
"""Validate the production R2 credential without printing secret material."""

from __future__ import annotations

import hashlib
import os
import uuid

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def main() -> None:
    endpoint = required("R2_ENDPOINT")
    bucket = required("R2_BUCKET")
    access_key = required("R2_ACCESS_KEY_ID")
    secret_key = required("R2_SECRET_ACCESS_KEY")

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )

    key = f"_migration_checks/{uuid.uuid4().hex}"
    payload = os.urandom(32)
    expected_digest = hashlib.sha256(payload).digest()
    created = False

    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="application/octet-stream",
        )
        created = True

        response = client.get_object(Bucket=bucket, Key=key)
        received = response["Body"].read()
        if hashlib.sha256(received).digest() != expected_digest:
            raise RuntimeError("R2 canary payload integrity check failed")

        listing = client.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=2)
        matches = [item.get("Key") for item in listing.get("Contents", [])]
        if matches != [key]:
            raise RuntimeError("R2 prefix-scoped listing did not return the canary")
    finally:
        if created:
            client.delete_object(Bucket=bucket, Key=key)

    try:
        buckets = client.list_buckets().get("Buckets", [])
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in {"AccessDenied", "Unauthorized", "InvalidAccessKeyId"}:
            raise
    else:
        unrelated = [item for item in buckets if item.get("Name") != bucket]
        if unrelated:
            raise RuntimeError("R2 credential can enumerate unrelated buckets")

    print("PASS: R2 put/get/list/delete works and no unrelated bucket is visible")


if __name__ == "__main__":
    main()
