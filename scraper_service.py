"""
scraper_service.py
Comment Radar용 외부 Instagram Post/Reel 댓글 수집기.

배포 예:
- Cloud Run / Render / Railway / Fly.io 등
- Apps Script의 EXTERNAL_SCRAPER_URL에는
  https://<host>/comments 를 넣습니다.

주의:
- Instagram 공개 Web GraphQL은 비공식/불안정 경로입니다.
- 401/403/429가 날 수 있습니다.
- 브랜드 공식 Instagram 로그인 계정은 이 서비스에 넣지 않는 것을 권장합니다.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from instagrapi import Client

app = FastAPI(title="Comment Radar Instagram Scraper")

API_TOKEN = os.getenv("API_TOKEN", "").strip()

# 2026 기준 public web 요청의 TLS fingerprint 문제를 줄이기 위한 옵션.
# 환경에서 curl transport 설치가 안 되면 아래 환경변수를 requests 로 바꾸세요.
PUBLIC_TRANSPORT = os.getenv("PUBLIC_TRANSPORT", "curl")
PUBLIC_IMPERSONATE = os.getenv("PUBLIC_IMPERSONATE", "chrome136")

client_kwargs: dict[str, Any] = {}
if PUBLIC_TRANSPORT == "curl":
    client_kwargs.update(
        public_transport="curl",
        public_transport_impersonate=PUBLIC_IMPERSONATE,
    )

cl = Client(**client_kwargs)
cl.public_request_retries_count = 2


class CommentRequest(BaseModel):
    url: str
    since: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=500)


def shortcode_from_url(url: str) -> str:
    m = re.search(r"instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)", url)
    if not m:
        raise ValueError("Instagram Post/Reel URL 형식이 아닙니다.")
    return m.group(1)


def parse_since(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def node_user(node: dict) -> str:
    owner = node.get("owner") or node.get("user") or {}
    return (
        owner.get("username")
        or owner.get("user_name")
        or node.get("username")
        or ""
    )


def node_created_at(node: dict) -> Optional[datetime]:
    raw = (
        node.get("created_at")
        or node.get("created_at_utc")
        or node.get("created_at_timestamp")
        or node.get("created_at_time")
    )
    if raw is None:
        return None

    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.utc)

    if isinstance(raw, str):
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw), tz=timezone.utc)
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    return None


def node_like_count(node: dict) -> int:
    for key in ("like_count", "likes_count"):
        if node.get(key) is not None:
            try:
                return int(node[key])
            except Exception:
                pass

    edge = node.get("edge_liked_by") or {}
    try:
        return int(edge.get("count") or 0)
    except Exception:
        return 0


def node_reply_count(node: dict) -> int:
    for key in ("reply_count", "replies_count", "child_comment_count"):
        if node.get(key) is not None:
            try:
                return int(node[key])
            except Exception:
                pass

    edge = node.get("edge_threaded_comments") or {}
    try:
        return int(edge.get("count") or 0)
    except Exception:
        return 0


def normalize_node(node: dict, is_reply: bool = False) -> dict:
    created = node_created_at(node)

    return {
        "id": str(node.get("id") or node.get("pk") or ""),
        "text": str(node.get("text") or ""),
        "author": node_user(node),
        "like_count": node_like_count(node),
        "reply_count": node_reply_count(node),
        "created_at": created.isoformat() if created else "",
        "is_reply": bool(is_reply),
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "comment-radar-instagram-scraper"}


@app.post("/comments")
def comments(
    req: CommentRequest,
    authorization: Optional[str] = Header(default=None),
) -> dict:
    if API_TOKEN:
        expected = f"Bearer {API_TOKEN}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        code = shortcode_from_url(req.url)
        since = parse_since(req.since)

        # 공식 API가 아닌 instagrapi public GraphQL helper.
        # 현재 라이브러리는 shortcode를 media PK로 변환해 댓글 GraphQL을 호출합니다.
        raw = cl.media_comments_public_gql(
            code,
            amount=req.limit,
            max_requests=max(1, (req.limit + 49) // 50),
        )

        result = []
        for node in raw:
            item = normalize_node(node, False)
            if not item["id"]:
                continue

            if since:
                created = node_created_at(node)
                if created and created < since:
                    continue

            result.append(item)

            # public GQL이 inline replies를 포함한 경우 함께 평탄화.
            threaded = node.get("edge_threaded_comments") or {}
            for edge in threaded.get("edges") or []:
                child = edge.get("node") or {}
                child_item = normalize_node(child, True)
                if not child_item["id"]:
                    continue
                if since:
                    child_created = node_created_at(child)
                    if child_created and child_created < since:
                        continue
                result.append(child_item)

        media_pk = str(cl.media_pk_from_code(code))

        return {
            "ok": True,
            "source": "instagrapi-public-gql",
            "shortcode": code,
            "media_pk": media_pk,
            "comment_count": len(raw),
            "comments": result,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
