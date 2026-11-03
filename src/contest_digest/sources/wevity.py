"""위비티(wevity.com) 어댑터. 목록과 상세 모두 정적 HTML."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from ..config import today_kst
from ..models import Detail, Listing
from .base import Source, html_to_text

BASE = "https://www.wevity.com/"
DATE_RANGE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})")


def list_url(page: int) -> str:
    """전체 분야 목록. IT 여부는 classify 단계에서 판단한다."""
    return f"{BASE}?c=find&s=1&gub=1&gp={page}"


def parse_dday(text: str, today: date) -> date | None:
    m = re.search(r"D-(\d+)", text)
    if m:
        return today + timedelta(days=int(m.group(1)))
    if "D-day" in text or "D-Day" in text or "오늘마감" in text:
        return today
    return None


def parse_list(html: str, today: date) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[Listing] = []
    for li in soup.select("ul.list > li"):
        if "top" in (li.get("class") or []):
            continue
        a = li.select_one("div.tit > a")
        if not a or "ix=" not in a.get("href", ""):
            continue
        ix = parse_qs(urlparse(a["href"]).query).get("ix", [""])[0]
        for span in a.select("span"):
            span.decompose()
        sub = li.select_one(".sub-tit")
        cats = []
        if sub:
            cats = [c.strip() for c in sub.get_text().split(":", 1)[-1].split(",") if c.strip()]
        day = li.select_one(".day")
        day_text = day.get_text(" ", strip=True) if day else ""
        if "마감" in day_text and "마감임박" not in day_text and not re.search(r"D-", day_text):
            continue  # 이미 마감
        organ = li.select_one(".organ")
        items.append(
            Listing(
                source="wevity",
                id=ix,
                title=a.get_text(" ", strip=True),
                url=f"{BASE}?c=find&s=1&gbn=view&ix={ix}",
                host=organ.get_text(strip=True) if organ else None,
                apply_end=parse_dday(day_text, today),
                categories=cats,
            )
        )
    return items


def parse_detail(html: str, listing: Listing) -> Detail:
    soup = BeautifulSoup(html, "html.parser")
    info = soup.select_one(".cd-info-list")
    info_text = html_to_text(str(info)) if info else ""
    body = soup.select_one("#viewContents")
    body_text = html_to_text(str(body)) if body else ""
    posters = [urljoin(BASE, img["src"]) for img in soup.select(".cd-area .thumb img") if img.get("src")]
    start = end = None
    m = DATE_RANGE.search(info_text)
    if m:
        start = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        end = datetime.strptime(m.group(2), "%Y-%m-%d").date()
    text = f"[요약 정보]\n{info_text}\n\n[상세 내용]\n{body_text}"
    return Detail(listing=listing, text=text, poster_urls=posters, apply_start=start,
                  apply_end=end, referer=listing.url)


class WevitySource(Source):
    name = "wevity"

    def collect(self) -> list[Listing]:
        today = self.today or today_kst().date()
        seen: dict[str, Listing] = {}
        for page in range(1, self.max_pages + 1):
            items = parse_list(self.client.get_text(list_url(page)), today)
            if not items:
                break
            for it in items:
                seen.setdefault(it.id, it)
        return list(seen.values())

    def fetch(self, listing: Listing) -> Detail:
        return parse_detail(self.client.get_text(listing.url), listing)
