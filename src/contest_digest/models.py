"""파이프라인 단계 사이에서 주고받는 데이터 구조."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel, Field


@dataclass
class Listing:
    """collect 단계 결과: 목록 페이지에서 얻은 공개 메타데이터."""

    source: str
    id: str
    title: str
    url: str
    host: str | None = None
    apply_end: date | None = None
    categories: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"


@dataclass
class Detail:
    """fetch 단계 결과: 상세 본문 텍스트와 포스터 URL."""

    listing: Listing
    text: str
    poster_urls: list[str] = field(default_factory=list)
    apply_start: date | None = None
    apply_end: date | None = None
    referer: str | None = None


class OtherDate(BaseModel):
    label: str
    date: str | None = None  # 하루짜리 일정의 날짜, 기간이면 시작일
    end_date: str | None = None  # 기간 일정의 종료일
    note: str | None = None


class Extraction(BaseModel):
    """VLM이 채우는 필드. 없는 정보는 null."""

    title: str
    host: str | None = None
    category: str | None = None
    topic: str | None = None
    apply_start: str | None = None
    apply_end: str | None = None
    other_dates: list[OtherDate] = Field(default_factory=list)
    prize_total: str | None = None
    prize_details: list[str] = Field(default_factory=list)
    eligibility: str | None = None
    team_size: str | None = None
    submission: str | None = None
    # 단발성 경진대회·공모전이고 수상·상금 등 보상이 있는가. 서포터즈·기자단·참여자 모집 등은 false
    is_competition: bool = True
    # 상세·포스터를 보고 판단한 IT·데이터·AI 여부. 목록 메타데이터만으로 한 classify 판정을 보정한다
    is_it: bool | None = None


class Contest(Extraction):
    """validate 이후 최종 레코드. URL은 수집 데이터로 채운다."""

    key: str
    source_url: str
    poster_urls: list[str] = Field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
