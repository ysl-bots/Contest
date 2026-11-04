"""dedupe 단계(마감·기처리 제외)와 키워드 기반 IT 판정(classify 대체 규칙)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .config import IT_KEYWORDS, IT_ONLY_CATEGORIES
from .models import Listing

_ASCII = re.compile(r"[a-z]+")


def _keyword_pattern(words: list[str]) -> re.Pattern:
    parts = []
    for w in words:
        if _ASCII.fullmatch(w):
            parts.append(rf"(?<![a-z]){re.escape(w)}(?![a-z])")
        else:
            parts.append(re.escape(w))
    return re.compile("|".join(parts), re.I)


_IT_PATTERN = _keyword_pattern(IT_KEYWORDS)


def is_it_related(listing: Listing) -> bool:
    if _IT_PATTERN.search(listing.title):
        return True
    cats = set(listing.categories)
    # "기타"만 있는 건 근거가 부족하므로 제외
    return bool(cats) and cats <= IT_ONLY_CATEGORIES and cats != {"기타"}


def is_open(listing: Listing, today: date) -> bool:
    """마감일을 모르면 일단 통과시키고, 상세 단계에서 다시 확인한다."""
    return listing.apply_end is None or listing.apply_end >= today


_BRACKETS = re.compile(r"[\[\(【<（][^\]\)】>）]*[\]\)】>）]")
_NON_WORD = re.compile(r"[^0-9a-z가-힣]")


def title_keys(title: str) -> set[str]:
    """소스 간 같은 공모전을 찾기 위한 정규화 제목들.

    소스마다 부제를 괄호·따옴표 등 다르게 표기하므로 ① 괄호 안 부연을 뺀 것 ② 기호만 뺀 것
    두 가지를 만들고, 하나라도 겹치면 같은 공모전으로 본다.
    """
    t = title.lower()
    return {k for k in (_NON_WORD.sub("", _BRACKETS.sub("", t)), _NON_WORD.sub("", t)) if k}


_YEAR = re.compile(r"(19|20)\d{2}년?|제?\d+(회|기|차)")
# 공모전 제목에 흔한 꼬리말. 소스마다 붙이거나 빼므로 비교에서 뺀다
_GENERIC = re.compile(r"(공모전|공모|경진대회|대회|챌린지|모집|접수|참가자|대국민|전국)")
_ORG_SUFFIX = re.compile(r"(주식회사|\(주\)|㈜|재단법인|사단법인|\(재\)|\(사\))")


def _norm(text: str) -> str:
    return _NON_WORD.sub("", text.lower())


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def _dice(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    return 2 * len(ga & gb) / (len(ga) + len(gb)) if ga and gb else 0.0


@dataclass(frozen=True)
class Fingerprint:
    """중복 판정용 메타데이터 요약: 정규화 제목·핵심 제목·주최·마감·소스."""

    keys: frozenset[str]
    core: str
    hosts: frozenset[str]
    apply_end: date | None
    source: str | None

    @classmethod
    def of(cls, title: str, host: str | None = None, apply_end: date | None = None,
           source: str | None = None) -> "Fingerprint":
        t = title.lower()
        brackets = [_norm(_ORG_SUFFIX.sub("", m.group()[1:-1])) for m in _BRACKETS.finditer(t)]
        core = _NON_WORD.sub("", _GENERIC.sub("", _YEAR.sub("", _BRACKETS.sub("", t))))
        hosts = {h for h in [_norm(_ORG_SUFFIX.sub("", host or "")), *brackets] if len(h) >= 2}
        return cls(frozenset(title_keys(title)), core, frozenset(hosts), apply_end, source)


def _host_match(a: Fingerprint, b: Fingerprint) -> bool:
    return any(x in y or y in x for x in a.hosts for y in b.hosts)


def is_same_contest(a: Fingerprint, b: Fingerprint) -> bool:
    """제목 유사도·주최·마감일을 종합해 같은 공모전인지 판단한다.

    - 정규화 제목이 같으면 같은 공모전 (기존 규칙)
    - 같은 소스의 다른 ID는 마감일까지 같을 때만 같은 공모전 후보로 본다 (같은 소스 재등록 대응)
    - 마감일이 둘 다 있고 다르면 거의 같은 제목일 때만 같은 것으로 본다 (회차·연도 구분)
    - 그 외에는 핵심 제목(연도·'공모전' 등 꼬리말·괄호 제외) 유사도에 주최·마감 일치를 더해 판정
    """
    if a.keys & b.keys:
        return True
    host = _host_match(a, b)
    ca, cb = a.core, b.core
    if host:
        # 주최명이 제목 대부분이면 같은 기관의 다른 공모전도 비슷해 보이므로 주최명을 빼고 비교한다
        for h in sorted(a.hosts | b.hosts, key=len, reverse=True):
            ca, cb = ca.replace(h, ""), cb.replace(h, "")
        if len(ca) < 4 or len(cb) < 4:
            ca, cb = a.core, b.core
    if not ca or not cb:
        return False
    da, db = set(re.findall(r"\d+", ca)), set(re.findall(r"\d+", cb))
    if da and db and da != db:  # 핵심 제목의 숫자(부문·회차 번호 등)가 다르면 별개
        return False
    sim = _dice(ca, cb)
    short, long_ = sorted((ca, cb), key=len)
    if len(short) >= 4 and short in long_:
        sim = max(sim, 0.85)
    ga, gb = _bigrams(ca), _bigrams(cb)
    if min(len(ga), len(gb)) >= 6:  # 어순만 바뀐 제목: 짧은 쪽 bigram이 대부분 긴 쪽에 있음
        sim = max(sim, 0.9 * len(ga & gb) / min(len(ga), len(gb)))
    same_end = a.apply_end is not None and a.apply_end == b.apply_end
    if a.source and a.source == b.source and not same_end:
        return False
    diff_end = a.apply_end is not None and b.apply_end is not None and not same_end
    host = _host_match(a, b)
    if diff_end:
        # 같은 공모전이라도 소스마다 마감 표기가 하루 정도 어긋날 수 있다
        if abs((a.apply_end - b.apply_end).days) > 1:
            return sim >= 0.95 and host
        return sim >= 0.8 and host
    score = sim + (0.1 if same_end else 0) + (0.1 if host else 0)
    return score >= 0.9


def select_new(listings: list[Listing], seen: set[str], today: date,
               history: list[Fingerprint] = ()) -> list[Listing]:
    """미처리·접수 중인 건을 마감순으로. 같은 공모전(다른 소스 포함)은 하나만 남긴다.

    history: 이전 실행에서 처리·대기 중인 건의 Fingerprint. 이들과 같은 공모전도 제외한다.
    """
    out: dict[str, Listing] = {}
    known = list(history)
    for it in sorted(listings, key=lambda x: x.key):
        if it.key in seen or it.key in out:
            continue
        fp = Fingerprint.of(it.title, it.host, it.apply_end, it.source)
        if any(is_same_contest(fp, h) for h in known):
            continue
        if is_open(it, today):
            out[it.key] = it
            known.append(fp)
    return sorted(out.values(), key=lambda x: (x.apply_end or date.max, x.key))
