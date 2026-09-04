"""
scraper_service.py
Comment Radar용 외부 Instagram Post/Reel 댓글 수집기.

동작:
1) Public GraphQL 우선 시도
2) 실패하면 별도 수집용 Instagram 계정으로 Private Mobile API fallback
3) 로그인/챌린지/레이트리밋/계정 제한을 구조화된 error_type으로 반환
   -> Apps Script가 Slack 장애 알림을 보낼 수 있음

Render Environment Variables
필수:
- API_TOKEN
선택(Private fallback을 쓰려면 필요):
- SCRAPER_IG_USERNAME
- SCRAPER_IG_PASSWORD

기존:
- PUBLIC_TRANSPORT=curl
- PUBLIC_IMPERSONATE=chrome136

선택:
- SCRAPER_SESSION_FILE=/tmp/comment_radar_ig_session.json
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from instagrapi import Client
from instagrapi import exceptions as igexc


app = FastAPI(title="Comment Radar Instagram Scraper")


# ─────────────────────────────────────
# Environment
# ─────────────────────────────────────

API_TOKEN = os.getenv("API_TOKEN", "").strip()

SCRAPER_IG_USERNAME = os.getenv("SCRAPER_IG_USERNAME", "").strip()
SCRAPER_IG_PASSWORD = os.getenv("SCRAPER_IG_PASSWORD", "").strip()

PUBLIC_TRANSPORT = os.getenv("PUBLIC_TRANSPORT", "curl").strip()
PUBLIC_IMPERSONATE = os.getenv("PUBLIC_IMPERSONATE", "chrome136").strip()

SESSION_FILE = Path(
    os.getenv(
        "SCRAPER_SESSION_FILE",
        "/tmp/comment_radar_ig_session.json",
    )
)


# ─────────────────────────────────────
# Clients
# ─────────────────────────────────────

public_kwargs: dict[str, Any] = {}

if PUBLIC_TRANSPORT == "curl":
    public_kwargs.update(
        public_transport="curl",
        public_transport_impersonate=PUBLIC_IMPERSONATE,
    )

public_cl = Client(**public_kwargs)
public_cl.public_request_retries_count = 1

_private_cl: Optional[Client] = None
_private_lock = threading.Lock()


# ─────────────────────────────────────
# Request model
# ─────────────────────────────────────

class CommentRequest(BaseModel):
    url: str
    since: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=500)


# ─────────────────────────────────────
# Error helpers
# ─────────────────────────────────────

def _is_exc(exc: Exception, name: str) -> bool:
    cls = getattr(igexc, name, None)
    return cls is not None and isinstance(exc, cls)


def classify_instagram_error(exc: Exception) -> tuple[str, str]:
    """
    Apps Script가 이해할 수 있는 오류 유형으로 통일.
    """
    cls_name = exc.__class__.__name__
    msg = str(exc)
    lower = msg.lower()

    if _is_exc(exc, "BadPassword") or "bad password" in lower:
        return "IG_BAD_PASSWORD", msg

    if _is_exc(exc, "ChallengeRequired") or "challenge_required" in lower:
        return "IG_CHALLENGE", msg

    if (
        _is_exc(exc, "TwoFactorRequired")
        or "two factor" in lower
        or "2fa required" in lower
        or "two_step_verification" in lower
    ):
        return "IG_2FA_REQUIRED", msg

    if (
        _is_exc(exc, "LoginRequired")
        or "login_required" in lower
        or "login required" in lower
    ):
        return "IG_LOGIN_REQUIRED", msg

    if (
        _is_exc(exc, "PleaseWaitFewMinutes")
        or "please wait a few minutes" in lower
    ):
        return "IG_RATE_LIMIT", msg

    if (
        _is_exc(exc, "ClientThrottledError")
        or _is_exc(exc, "RateLimitError")
        or "rate limit" in lower
        or "429" in lower
    ):
        return "IG_RATE_LIMIT", msg

    if _is_exc(exc, "FeedbackRequired"):
        feedback = ""
        try:
            feedback = str(getattr(exc, "message", "") or "")
        except Exception:
            pass
        combined = (msg + " " + feedback).lower()

        if (
            "temporarily blocked" in combined
            or "action was blocked" in combined
            or "restrict certain activity" in combined
        ):
            return "IG_ACCOUNT_BLOCKED", msg

        return "IG_FEEDBACK_REQUIRED", msg

    if (
        _is_exc(exc, "SentryBlock")
        or "sentry_block" in lower
        or "user_blocked" in lower
        or "automated behavior" in lower
        or "temporarily blocked" in lower
        or "account has been blocked" in lower
    ):
        return "IG_ACCOUNT_BLOCKED", msg

    if (
        _is_exc(exc, "ClientUnauthorizedError")
        or _is_exc(exc, "ClientForbiddenError")
        or "403" in lower
        or "401" in lower
    ):
        return "IG_ACCESS_BLOCKED", msg

    if "auth_platform" in lower:
        return "IG_CHALLENGE", msg

    return "IG_FETCH_FAILED", f"{cls_name}: {msg}"


def raise_typed_http(
    error_type: str,
    message: str,
    *,
    public_error: Optional[str] = None,
    status_code: int = 502,
) -> None:
    detail: dict[str, Any] = {
        "error_type": error_type,
        "message": str(message)[:1000],
    }

    if public_error:
        detail["public_error"] = str(public_error)[:800]

    raise HTTPException(
        status_code=status_code,
        detail=detail,
    )


# ─────────────────────────────────────
# URL / time
# ─────────────────────────────────────

def shortcode_from_url(url: str) -> str:
    m = re.search(
        r"instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)",
        url,
    )

    if not m:
        raise ValueError("Instagram Post/Reel URL 형식이 아닙니다.")

    return m.group(1)


def parse_since(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    dt = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def iso_datetime(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(
            value,
            tz=timezone.utc,
        ).isoformat()

    if isinstance(value, str):
        if value.isdigit():
            return datetime.fromtimestamp(
                int(value),
                tz=timezone.utc,
            ).isoformat()

        try:
            dt = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            return value

    return ""


def datetime_for_filter(value: Any) -> Optional[datetime]:
    text = iso_datetime(value)

    if not text:
        return None

    try:
        dt = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# ─────────────────────────────────────
# Public GraphQL normalization
# ─────────────────────────────────────

def dict_username(node: dict[str, Any]) -> str:
    owner = node.get("owner") or node.get("user") or {}

    return str(
        owner.get("username")
        or owner.get("user_name")
        or node.get("username")
        or ""
    )


def dict_created(node: dict[str, Any]) -> Any:
    return (
        node.get("created_at_utc")
        or node.get("created_at")
        or node.get("created_at_timestamp")
        or node.get("created_at_time")
    )


def dict_like_count(node: dict[str, Any]) -> int:
    for key in ("like_count", "likes_count"):
        value = node.get(key)

        if value is not None:
            try:
                return int(value)
            except Exception:
                pass

    edge = node.get("edge_liked_by") or {}

    try:
        return int(edge.get("count") or 0)
    except Exception:
        return 0


def dict_reply_count(node: dict[str, Any]) -> int:
    for key in (
        "reply_count",
        "replies_count",
        "child_comment_count",
    ):
        value = node.get(key)

        if value is not None:
            try:
                return int(value)
            except Exception:
                pass

    edge = node.get("edge_threaded_comments") or {}

    try:
        return int(edge.get("count") or 0)
    except Exception:
        return 0


def normalize_public_node(
    node: dict[str, Any],
    *,
    is_reply: bool = False,
) -> dict[str, Any]:
    return {
        "id": str(
            node.get("id")
            or node.get("pk")
            or ""
        ),
        "text": str(node.get("text") or ""),
        "author": dict_username(node),
        "like_count": dict_like_count(node),
        "reply_count": (
            0
            if is_reply
            else dict_reply_count(node)
        ),
        "created_at": iso_datetime(
            dict_created(node)
        ),
        "is_reply": bool(is_reply),
    }


def flatten_public_comments(
    raw: list[dict[str, Any]],
    since: Optional[datetime],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for node in raw:
        item = normalize_public_node(
            node,
            is_reply=False,
        )

        created = datetime_for_filter(
            item["created_at"]
        )

        if (
            item["id"]
            and (
                since is None
                or created is None
                or created >= since
            )
        ):
            result.append(item)

        threaded = (
            node.get("edge_threaded_comments")
            or {}
        )

        for edge in threaded.get("edges") or []:
            child = edge.get("node") or {}

            child_item = normalize_public_node(
                child,
                is_reply=True,
            )

            child_created = datetime_for_filter(
                child_item["created_at"]
            )

            if (
                child_item["id"]
                and (
                    since is None
                    or child_created is None
                    or child_created >= since
                )
            ):
                result.append(child_item)

    return result


# ─────────────────────────────────────
# Private session
# ─────────────────────────────────────

def _new_private_client() -> Client:
    cl = Client()

    # 요청 폭주 방지
    cl.request_timeout = 30

    return cl


def get_private_client(
    *,
    force_fresh: bool = False,
) -> Client:
    """
    세션 파일이 있으면 재사용.
    로그인 성공 후 다시 dump.
    """
    global _private_cl

    if not SCRAPER_IG_USERNAME or not SCRAPER_IG_PASSWORD:
        raise RuntimeError(
            "SCRAPER_IG_USERNAME / SCRAPER_IG_PASSWORD 미설정"
        )

    with _private_lock:
        if _private_cl is not None and not force_fresh:
            return _private_cl

        cl = _new_private_client()

        if (
            not force_fresh
            and SESSION_FILE.exists()
        ):
            try:
                cl.load_settings(str(SESSION_FILE))
            except Exception:
                pass

        try:
            cl.login(
                SCRAPER_IG_USERNAME,
                SCRAPER_IG_PASSWORD,
            )
        except Exception:
            # 손상된/만료 세션일 수 있으므로
            # 한 번만 세션 없는 새 클라이언트로 재시도
            if not force_fresh and SESSION_FILE.exists():
                try:
                    SESSION_FILE.unlink(missing_ok=True)
                except Exception:
                    pass

                cl = _new_private_client()

                cl.login(
                    SCRAPER_IG_USERNAME,
                    SCRAPER_IG_PASSWORD,
                )
            else:
                raise

        try:
            SESSION_FILE.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            cl.dump_settings(str(SESSION_FILE))
        except Exception:
            # 세션 저장 실패가 수집 자체를 막지는 않음
            pass

        _private_cl = cl
        return cl


# ─────────────────────────────────────
# Private comment normalization
# ─────────────────────────────────────

def normalize_private_comment(
    comment: Any,
) -> dict[str, Any]:
    user = getattr(comment, "user", None)

    return {
        "id": str(
            getattr(comment, "pk", "")
            or ""
        ),
        "text": str(
            getattr(comment, "text", "")
            or ""
        ),
        "author": str(
            getattr(user, "username", "")
            or ""
        ),
        "like_count": int(
            getattr(comment, "like_count", 0)
            or 0
        ),
        "reply_count": 0,
        "created_at": iso_datetime(
            getattr(
                comment,
                "created_at_utc",
                None,
            )
        ),
        "is_reply": bool(
            getattr(
                comment,
                "replied_to_comment_id",
                None,
            )
        ),
    }


def private_comments(
    code: str,
    amount: int,
    since: Optional[datetime],
) -> tuple[str, list[dict[str, Any]]]:
    """
    Private Mobile API.
    login_required가 나면 세션을 새로 만든 뒤 1회 재시도.
    """
    global _private_cl

    media_pk = str(
        public_cl.media_pk_from_code(code)
    )

    cl = get_private_client()

    try:
        raw = cl.media_comments_v1(
            media_pk,
            amount=amount,
        )

    except Exception as exc:
        error_type, _ = classify_instagram_error(exc)

        if error_type == "IG_LOGIN_REQUIRED":
            with _private_lock:
                _private_cl = None

            cl = get_private_client(
                force_fresh=True
            )

            raw = cl.media_comments_v1(
                media_pk,
                amount=amount,
            )
        else:
            raise

    result: list[dict[str, Any]] = []

    for comment in raw:
        item = normalize_private_comment(comment)

        created = datetime_for_filter(
            item["created_at"]
        )

        if (
            item["id"]
            and (
                since is None
                or created is None
                or created >= since
            )
        ):
            result.append(item)

    return media_pk, result


# ─────────────────────────────────────
# Routes
# ─────────────────────────────────────

@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "comment-radar-instagram-scraper",
        "private_fallback_configured": bool(
            SCRAPER_IG_USERNAME
            and SCRAPER_IG_PASSWORD
        ),
    }


@app.post("/comments")
def comments(
    req: CommentRequest,
    authorization: Optional[str] = Header(
        default=None
    ),
) -> dict[str, Any]:

    # Apps Script ↔ scraper 인증
    if API_TOKEN:
        expected = f"Bearer {API_TOKEN}"

        if authorization != expected:
            raise HTTPException(
                status_code=401,
                detail={
                    "error_type": "SCRAPER_AUTH_ERROR",
                    "message": "Unauthorized",
                },
            )

    try:
        code = shortcode_from_url(req.url)
        since = parse_since(req.since)

    except Exception as exc:
        raise_typed_http(
            "INVALID_INSTAGRAM_URL",
            str(exc),
            status_code=400,
        )

    # ─────────────────────────────
    # 1. Public GraphQL
    # ─────────────────────────────

    public_error: Optional[str] = None

    try:
        raw = public_cl.media_comments_public_gql(
            code,
            amount=req.limit,
            max_requests=max(
                1,
                (req.limit + 49) // 50,
            ),
        )

        normalized = flatten_public_comments(
            raw,
            since,
        )

        media_pk = str(
            public_cl.media_pk_from_code(code)
        )

        return {
            "ok": True,
            "source": "instagrapi-public-gql",
            "shortcode": code,
            "media_pk": media_pk,
            "comment_count": len(raw),
            "comments": normalized,
        }

    except Exception as exc:
        public_error = (
            f"{exc.__class__.__name__}: {exc}"
        )

    # ─────────────────────────────
    # 2. Private API fallback
    # ─────────────────────────────

    if not (
        SCRAPER_IG_USERNAME
        and SCRAPER_IG_PASSWORD
    ):
        raise_typed_http(
            "PUBLIC_GQL_FAILED",
            (
                "Public GraphQL 실패. "
                "Private fallback용 SCRAPER_IG_USERNAME / "
                "SCRAPER_IG_PASSWORD가 설정되지 않았습니다."
            ),
            public_error=public_error,
        )

    try:
        media_pk, normalized = private_comments(
            code,
            req.limit,
            since,
        )

        return {
            "ok": True,
            "source": "instagrapi-private-v1",
            "shortcode": code,
            "media_pk": media_pk,
            "comment_count": len(normalized),
            "comments": normalized,
            "public_fallback_reason": public_error,
        }

    except Exception as exc:
        error_type, message = classify_instagram_error(
            exc
        )

        status_code = 502

        if error_type in {
            "IG_RATE_LIMIT",
            "IG_ACCOUNT_BLOCKED",
            "IG_CHALLENGE",
            "IG_2FA_REQUIRED",
        }:
            status_code = 503

        raise_typed_http(
            error_type,
            message,
            public_error=public_error,
            status_code=status_code,
        )
