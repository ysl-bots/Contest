"""처리한 공모전 ID를 저장소 내 JSON으로 관리한다.

status: sent(발송) · skipped(마감 등으로 발송 안 함) · pending(분류는 끝났고 발행 대기 중)
it: IT 트랙(true)·Non-IT 트랙(false) 구분.
moved: 상세를 보고 트랙을 한 번 옮긴 건. 다시 옮기지 않아 왕복을 막는다.
pending은 목록 메타데이터를 함께 저장해, 목록 페이지에서 밀려나도 다음 실행에서 우선 처리한다.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from .models import Listing

PENDING = "pending"


class State:
    def __init__(self, data: dict | None = None):
        data = data or {}
        self.items: dict[str, dict] = data.get("items", {})
        self.last_run: str | None = data.get("last_run")

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls()
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def seen(self) -> set[str]:
        """처리가 끝난(다시 볼 필요 없는) 키. pending은 포함하지 않는다."""
        return {k for k, v in self.items.items() if v.get("status") != PENDING}

    def pending(self, is_it: bool | None = None) -> list[Listing]:
        """발행 대기 건. is_it을 주면 해당 트랙만."""
        out = []
        for v in self.items.values():
            if is_it is not None and bool(v.get("it")) != is_it:
                continue
            if v.get("status") == PENDING and v.get("listing"):
                d = dict(v["listing"])
                d["apply_end"] = date.fromisoformat(d["apply_end"]) if d.get("apply_end") else None
                out.append(Listing(**d))
        return out

    def was_moved(self, key: str) -> bool:
        return bool(self.items.get(key, {}).get("moved"))

    def fingerprints(self) -> list:
        """중복 판정용으로 저장된 모든 건(처리·대기)의 Fingerprint."""
        from .filters import Fingerprint

        out = []
        for key, v in self.items.items():
            if not v.get("title"):
                continue
            end = date.fromisoformat(v["apply_end"]) if v.get("apply_end") else None
            host = v.get("host") or (v.get("listing") or {}).get("host")
            out.append(Fingerprint.of(v["title"], host, end, key.split(":", 1)[0]))
        return out

    def mark(self, key: str, today: date, apply_end: date | None, issue_number: int | None,
             title: str | None = None, is_it: bool | None = None, status: str | None = None,
             listing: Listing | None = None, moved: bool = False, host: str | None = None) -> None:
        prev = self.items.get(key, {})
        item = {
            "title": title,
            "host": host or prev.get("host") or (listing.host if listing else None),
            "first_seen": prev.get("first_seen") or today.isoformat(),
            "apply_end": apply_end.isoformat() if apply_end else None,
            "it": is_it,
            "status": status or ("sent" if issue_number else "skipped"),
            "issue": issue_number,
        }
        if moved or prev.get("moved"):
            item["moved"] = True
        if item["status"] == PENDING and listing:
            item["listing"] = {**listing.__dict__,
                               "apply_end": listing.apply_end.isoformat() if listing.apply_end else None}
        self.items[key] = item

    def prune(self, today: date, retention_days: int) -> None:
        """마감 후 retention_days가 지난 항목을 지운다. 마감일을 모르면 first_seen 기준."""
        cutoff = today - timedelta(days=retention_days)
        keep = {}
        for key, v in self.items.items():
            ref = v.get("apply_end") or v.get("first_seen")
            if ref and date.fromisoformat(ref) < cutoff:
                continue
            keep[key] = v
        self.items = keep

    def save(self, path: Path, today: date) -> None:
        # 신규 0건이어도 last_run이 바뀌어 커밋이 생긴다 (scheduled workflow 비활성화 방지)
        self.last_run = today.isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"last_run": self.last_run, "items": dict(sorted(self.items.items()))}
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
