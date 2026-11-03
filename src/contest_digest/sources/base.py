"""소스 어댑터 공통 인터페이스."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from bs4 import BeautifulSoup

from ..http import HttpClient
from ..models import Detail, Listing


class Source(ABC):
    name: str

    def __init__(self, client: HttpClient, max_pages: int = 3, today: date | None = None):
        self.client = client
        self.max_pages = max_pages
        self.today = today  # D-day 등 상대 날짜 해석 기준. None이면 실행 시점(KST)

    @abstractmethod
    def collect(self) -> list[Listing]:
        """목록 페이지들을 읽어 메타데이터를 반환한다."""

    @abstractmethod
    def fetch(self, listing: Listing) -> Detail:
        """상세 페이지에서 본문 텍스트와 포스터 URL을 가져온다."""


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)
