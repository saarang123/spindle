"""S3-backed ArtifactStore via aioboto3.

Works against MinIO, AWS S3, Cloudflare R2, Backblaze B2 — same SDK, same
wire protocol. The only deployment difference is the endpoint URL and
credentials.

URI scheme: "s3://<bucket>/<key>".

Bucket auto-creation: lazy on first put. The bucket is created if missing
and we have CreateBucket permission. After first success the result is
cached on the instance so subsequent puts don't re-check.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

import aioboto3
from botocore.exceptions import ClientError

from spindle_core.artifacts.protocol import ArtifactStat


class S3ArtifactStore:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ) -> None:
        self._endpoint = endpoint
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region

        self._session = aioboto3.Session()
        self._bucket_ensured = False
        self._bucket_lock = asyncio.Lock()

    def _client(self) -> Any:
        # Returns the async context manager for an S3 client.
        # aioboto3 recommends per-operation context to manage the underlying
        # aiohttp session; the boto connection pool keeps things efficient.
        # Return type is `Any` because aioboto3's stubs don't expose the
        # client class shape — methods are pyright-invisible.
        return self._session.client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self._region,
        )

    async def _ensure_bucket(self) -> None:
        if self._bucket_ensured:
            return
        async with self._bucket_lock:
            if self._bucket_ensured:
                return
            async with self._client() as s3:
                try:
                    await s3.head_bucket(Bucket=self._bucket)
                except ClientError as e:
                    code = e.response.get("Error", {}).get("Code", "")
                    # MinIO returns "404"; AWS returns "NoSuchBucket".
                    if code in {"404", "NoSuchBucket", "NotFound"}:
                        await s3.create_bucket(Bucket=self._bucket)
                    else:
                        raise
            self._bucket_ensured = True

    @staticmethod
    def _parse_uri(uri: str) -> tuple[str, str]:
        parsed = urlparse(uri)
        if parsed.scheme != "s3":
            raise ValueError(f"not an s3:// URI: {uri!r}")
        return parsed.netloc, parsed.path.lstrip("/")

    def _uri(self, key: str) -> str:
        return f"s3://{self._bucket}/{key}"

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str:
        await self._ensure_bucket()
        kwargs: dict[str, object] = {"Bucket": self._bucket, "Key": key, "Body": data}
        if content_type:
            kwargs["ContentType"] = content_type
        if metadata:
            kwargs["Metadata"] = metadata
        async with self._client() as s3:
            await s3.put_object(**kwargs)
        return self._uri(key)

    async def get(self, uri: str) -> bytes:
        bucket, key = self._parse_uri(uri)
        async with self._client() as s3:
            try:
                resp = await s3.get_object(Bucket=bucket, Key=key)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code in {"NoSuchKey", "404", "NotFound"}:
                    raise FileNotFoundError(uri) from e
                raise
            async with resp["Body"] as body:
                return await body.read()

    async def stat(self, uri: str) -> ArtifactStat | None:
        bucket, key = self._parse_uri(uri)
        async with self._client() as s3:
            try:
                resp = await s3.head_object(Bucket=bucket, Key=key)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code in {"NoSuchKey", "404", "NotFound"}:
                    return None
                raise
        return ArtifactStat(
            uri=uri,
            size_bytes=int(resp["ContentLength"]),
            content_type=resp.get("ContentType"),
            etag=(resp.get("ETag") or "").strip('"') or None,
        )

    async def delete(self, uri: str) -> None:
        bucket, key = self._parse_uri(uri)
        async with self._client() as s3:
            # S3 delete is idempotent: deleting a missing key returns success.
            await s3.delete_object(Bucket=bucket, Key=key)

    async def signed_url(self, uri: str, *, ttl_seconds: int = 3600) -> str | None:
        bucket, key = self._parse_uri(uri)
        async with self._client() as s3:
            url = await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=ttl_seconds,
            )
        return url
