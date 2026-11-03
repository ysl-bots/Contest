"""extract 단계: OpenRouter VLM에 텍스트와 포스터를 넣어 구조화한다."""

from __future__ import annotations

import base64
import io
import json
import logging
from datetime import date

from PIL import Image
from pydantic import ValidationError as PydanticError

from .config import Settings
from .http import HttpClient
from .llm import ModelCaps, chat_json
from .models import Contest, Detail, Extraction
from .validate import ValidationError, check, finalize

log = logging.getLogger(__name__)

_STR = {"type": ["string", "null"]}
JSON_SCHEMA = {
    "name": "contest",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "title", "host", "category", "topic", "apply_start", "apply_end", "other_dates",
            "prize_total", "prize_details", "eligibility", "team_size", "submission", "is_competition", "is_it",
        ],
        "properties": {
            "title": {"type": "string"},
            "host": _STR,
            "category": _STR,
            "topic": _STR,
            "apply_start": _STR,
            "apply_end": _STR,
            "other_dates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["label", "date", "end_date", "note"],
                    "properties": {"label": {"type": "string"}, "date": _STR, "end_date": _STR, "note": _STR},
                },
            },
            "prize_total": _STR,
            "prize_details": {"type": "array", "items": {"type": "string"}},
            "eligibility": _STR,
            "team_size": _STR,
            "submission": _STR,
            "is_competition": {"type": "boolean"},
            "is_it": {"type": "boolean"},
        },
    },
}


def image_to_data_url(raw: bytes, max_side: int) -> str:
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def load_posters(detail: Detail, http: HttpClient, settings: Settings) -> tuple[list[str], list[str]]:
    """포스터를 다운로드해 data URL로 만든다. 실패한 건 사유만 남기고 건너뛴다."""
    urls, errors = [], []
    for url in detail.poster_urls[: settings.max_posters]:
        try:
            resp = http.get(url, referer=detail.referer)
            urls.append(image_to_data_url(resp.content, settings.image_max_side))
        except Exception as exc:  # noqa: BLE001 - 포스터 실패는 치명적이지 않음
            errors.append(f"포스터 로드 실패: {url} ({type(exc).__name__})")
    return urls, errors


def build_messages(prompt: str, detail: Detail, images: list[str], char_limit: int, feedback: str | None = None):
    text = detail.text[:char_limit]
    content: list[dict] = [{"type": "text", "text": f"{prompt}\n\n[공고 텍스트]\n{text}"}]
    if feedback:
        content.append({"type": "text", "text": f"이전 출력이 검증에 실패했습니다: {feedback}\n규칙에 맞게 다시 출력하세요."})
    content += [{"type": "image_url", "image_url": {"url": u}} for u in images]
    return [{"role": "user", "content": content}]


class Extractor:
    def __init__(self, settings: Settings, http: HttpClient, client=None, caps: ModelCaps | None = None):
        self.settings = settings
        self.http = http
        self.prompt = settings.prompt_path.read_text(encoding="utf-8")
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=settings.openrouter_api_key, base_url=settings.openrouter_base_url)
        self.client = client
        self.caps = caps or ModelCaps(settings.openrouter_base_url)
        self.calls = 0

    def _call(self, model: str, messages) -> Extraction:
        self.calls += 1
        return Extraction.model_validate(chat_json(self.client, model, messages, JSON_SCHEMA, self.caps))

    def extract(self, detail: Detail, today: date) -> Contest:
        images, reasons = load_posters(detail, self.http, self.settings)
        if not detail.poster_urls:
            reasons.append("포스터 없음 (텍스트만 사용)")
        # 주모델 → 주모델 재시도(검증 피드백 포함) → 대체 모델
        plan = [self.settings.vlm_model, self.settings.vlm_model, self.settings.vlm_fallback_model]
        feedback = None
        for model in plan:
            try:
                ext = self._call(model, build_messages(self.prompt, detail, images, self.settings.text_char_limit, feedback))
                check(ext, today)
                info = [r for r in reasons if not r.startswith("포스터 없음")]
                return finalize(ext, detail, info)
            except (ValidationError, PydanticError, json.JSONDecodeError) as exc:
                feedback = str(exc)[:500]
                log.warning("%s 추출 검증 실패 (%s): %s", detail.listing.key, model, feedback)
            except Exception as exc:  # noqa: BLE001 - API 오류도 다음 모델로 넘긴다
                feedback = None
                log.warning("%s 추출 호출 실패 (%s): %s", detail.listing.key, model, exc)
        return finalize(None, detail, reasons + ["VLM 추출 실패, 목록 메타데이터만 표시"])


def metadata_only(detail: Detail) -> Contest:
    """API 키 없이 실행할 때(dry-run 등) 쓰는 대체 경로."""
    return finalize(None, detail, ["VLM 미사용 (메타데이터만)"])
