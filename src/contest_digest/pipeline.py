"""collect → dedupe → classify(트랙 배정) / 트랙별 fetch → extract/validate → render 오케스트레이션.

classify 단계는 신규 건을 IT·Non-IT로 나눠 모두 pending으로 상태에 넣는다.
process 단계는 트랙 하나의 pending을 처리하므로 트랙마다 별도 job으로 나눠 돌릴 수 있다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from .classify import Classifier, Verdict, classify_by_keyword
from .config import NON_IT_CATEGORIES, NOT_COMPETITION_KEYWORDS, Settings
from .extract import Extractor, metadata_only
from .filters import select_new
from .http import HttpClient
from .models import Contest, Listing
from .render import render, title
from .sources import SOURCES
from .state import PENDING, State

log = logging.getLogger(__name__)


@dataclass
class Mark:
    """상태 파일에 기록할 항목. 발송한 건은 Issue 번호가 나중에 채워진다."""

    listing: Listing
    is_it: bool
    apply_end: date | None = None
    status: str = "skipped"  # sent · skipped · pending


@dataclass
class ClassifyResult:
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    collected: int = 0
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def it(self) -> int:
        return sum(1 for v in self.verdicts.values() if v.is_it)

    @property
    def non_it(self) -> int:
        return len(self.verdicts) - self.it


@dataclass
class RunResult:
    track: str = "it"
    contests: list[Contest] = field(default_factory=list)
    excluded: list[Listing] = field(default_factory=list)  # 공모전 기준 미달. 본문 맨 아래 접어서 보여준다
    marks: list[Mark] = field(default_factory=list)
    title: str = ""
    body: str = ""

    @property
    def deferred(self) -> int:
        return sum(1 for m in self.marks if m.status == PENDING and m.is_it == (self.track == "it"))

    @property
    def moved(self) -> int:
        """상세를 보고 다른 트랙으로 넘긴 건수."""
        return sum(1 for m in self.marks if m.status == PENDING and m.is_it != (self.track == "it"))


def collect(http: HttpClient, settings: Settings, names: list[str],
            today: date | None = None) -> tuple[list[Listing], dict[str, str]]:
    listings, failures = [], {}
    for name in names:
        try:
            got = SOURCES[name](http, settings.max_pages, today).collect()
            log.info("%s: %d건 수집", name, len(got))
            listings += got
        except Exception as exc:  # noqa: BLE001 - 소스 하나가 실패해도 계속
            log.exception("%s 수집 실패", name)
            failures[name] = f"{type(exc).__name__}: {exc}"
    return listings, failures


def _by_deadline(items: list[Listing]) -> list[Listing]:
    return sorted(items, key=lambda x: (x.apply_end or date.max, x.key))


def classify_new(settings: Settings, http: HttpClient, state: State, today: date, names: list[str],
                 use_llm: bool = True, classifier: Classifier | None = None) -> ClassifyResult:
    """신규 건을 수집·분류해 트랙(it 여부)과 함께 pending으로 state에 넣는다."""
    result = ClassifyResult()
    listings, result.failures = collect(http, settings, names, today)
    result.collected = len(listings)
    pending_keys = {it.key for it in state.pending()}
    new = select_new(listings, state.seen() | pending_keys, today, state.seen_titles())

    if use_llm and classifier is None:
        classifier = Classifier(settings)
    result.verdicts = classifier.classify(new) if classifier else classify_by_keyword(new)
    result.titles = {it.key: it.title for it in new}
    for listing in new:
        state.mark(listing.key, today, listing.apply_end, None, title=listing.title,
                   is_it=result.verdicts[listing.key].is_it, status=PENDING, listing=listing)
    for src, err in result.failures.items():
        log.warning("소스 수집 실패 %s: %s", src, err)
    log.info("수집 %d건 → 신규 %d건 (IT %d, Non-IT %d)", result.collected, len(new), result.it, result.non_it)
    return result


def process(settings: Settings, http: HttpClient, state: State, today: date, track: str,
            use_llm: bool = True, extractor: Extractor | None = None) -> RunResult:
    """트랙 하나의 pending을 마감순으로 최대 max_items건 상세 수집·추출·렌더링한다."""
    is_it = track == "it"
    result = RunResult(track)
    queue = []
    for it in _by_deadline(state.pending(is_it)):
        if it.apply_end and it.apply_end < today:
            result.marks.append(Mark(it, is_it, it.apply_end))  # 대기 중 마감
        elif any(w in it.title for w in NOT_COMPETITION_KEYWORDS):
            result.excluded.append(it)  # 서포터즈 등 활동형 모집
            result.marks.append(Mark(it, is_it, it.apply_end))
        else:
            queue.append(it)
    todo, overflow = queue[: settings.max_items], queue[settings.max_items:]
    for listing in overflow:
        result.marks.append(Mark(listing, is_it, listing.apply_end, PENDING))
    log.info("[%s] 대기 %d건 → 처리 %d건, 다음으로 이월 %d건", track, len(queue), len(todo), len(overflow))

    if use_llm and extractor is None:
        extractor = Extractor(settings, http)
    sources: dict = {}
    for listing in todo:
        try:
            if listing.source not in sources:
                sources[listing.source] = SOURCES[listing.source](http, settings.max_pages, today)
            detail = sources[listing.source].fetch(listing)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 상세 수집 실패: %s", listing.key, exc)
            result.marks.append(Mark(listing, is_it, listing.apply_end, PENDING))  # 다음 실행에서 재시도
            continue
        end = detail.apply_end or listing.apply_end
        if end and end < today:
            result.marks.append(Mark(listing, is_it, end))
            continue
        contest = extractor.extract(detail, today) if extractor else metadata_only(detail)
        if is_it and contest.category in NON_IT_CATEGORIES:
            # 상세를 보고 나서야 비IT로 드러난 경우 Non-IT 트랙으로 넘긴다 (반대 방향은 하지 않아 왕복을 막는다)
            result.marks.append(Mark(listing, False, end, PENDING))
            continue
        if not contest.is_competition:
            # 상세를 보고 단발성 대회·보상 기준에 맞지 않는 모집으로 드러난 경우
            log.info("%s 공모전 아님으로 제외: %s", listing.key, listing.title[:40])
            result.excluded.append(listing)
            result.marks.append(Mark(listing, is_it, end))
            continue
        result.contests.append(contest)
        result.marks.append(Mark(listing, is_it, end, "sent"))

    result.title = title(result.contests, today, track)
    result.body = render(result.contests, today, settings.urgent_days, track, result.excluded)
    return result


def run(settings: Settings, http: HttpClient, state: State, today: date, names: list[str],
        use_llm: bool = True, extractor: Extractor | None = None,
        classifier: Classifier | None = None, track: str = "it") -> RunResult:
    """classify + 트랙 하나 처리 (테스트·단일 실행용)."""
    classify_new(settings, http, state, today, names, use_llm, classifier)
    return process(settings, http, state, today, track, use_llm, extractor)


def save_marks(state: State, result: RunResult, today: date, issue: int | None) -> None:
    for m in result.marks:
        sent = m.status == "sent"
        state.mark(m.listing.key, today, m.apply_end, issue if sent else None, title=m.listing.title,
                   is_it=m.is_it, status=m.status if not sent or issue else PENDING, listing=m.listing)
