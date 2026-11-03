from contest_digest.config import Settings
from contest_digest.models import Contest
from contest_digest.pipeline import process, run
from contest_digest.render import render
from contest_digest.state import State


def C(key, end, **kw):
    return Contest(title=kw.pop("title", key), key=key, source_url=f"https://x/{key}", apply_end=end, **kw)


def test_render_sections(today):
    body = render(
        [C("wevity:2", "2026-11-20"), C("wevity:1", "2026-10-29", topic="주제 | 파이프"),
         C("linkareer:3", None, needs_review=True, review_reasons=["마감일 확인 불가"])],
        today,
    )
    urgent, rest = body.split("## 📋")
    assert "wevity:1" in urgent and "D-3" in urgent and "wevity:2" not in urgent
    assert rest.index("wevity:2") < rest.index("linkareer:3")  # 마감일 미확인은 맨 뒤
    assert "주제 \\| 파이프" in body
    assert "⚠️ 확인 필요" in body
    assert "운영 메모" not in body and "Watch" not in body and "Unwatch" not in body


def test_offline_pipeline_no_llm(http, today):
    s = Settings()
    s.request_delay = 0
    result = run(s, http, State(), today, ["wevity", "linkareer"], use_llm=False)
    keys = [c.key for c in result.contests]
    assert set(keys) == {"wevity:900001", "wevity:900002", "linkareer:800001"}
    assert "가을 시 낭송" not in result.body and "슬로건" not in result.body
    assert result.title.startswith("주간 공모전 - ") and result.title.endswith("째주 IT·AI 공모전")


def test_source_failure_isolated(http, today, monkeypatch):
    from contest_digest.sources import linkareer

    def boom(self):
        raise ValueError("구조 변경")

    monkeypatch.setattr(linkareer.LinkareerSource, "collect", boom)
    from contest_digest.pipeline import classify_new

    state = State()
    cr = classify_new(Settings(), http, state, today, ["wevity", "linkareer"], use_llm=False)
    assert "linkareer" in cr.failures
    result = process(Settings(), http, state, today, "it", use_llm=False)
    assert len(result.contests) == 2


def test_max_items_defers_to_pending_and_prioritizes_next_run(http, today, monkeypatch):
    from datetime import timedelta

    from contest_digest.pipeline import save_marks
    from contest_digest.sources import linkareer, wevity

    s = Settings()
    s.max_items = 1
    state = State()
    result = run(s, http, state, today, ["wevity", "linkareer"], use_llm=False)
    assert [c.key for c in result.contests] == ["wevity:900001"] and result.deferred == 2
    save_marks(state, result, today, issue=1)
    assert state.items["wevity:900002"]["status"] == "pending"
    assert state.items["wevity:900002"]["listing"]["url"].endswith("ix=900002")
    assert "wevity:900002" not in state.seen()

    # 다음 주: 목록에서 사라져도(수집 0건) pending은 재분류 없이 우선 처리된다
    monkeypatch.setattr(wevity.WevitySource, "collect", lambda self: [])
    monkeypatch.setattr(linkareer.LinkareerSource, "collect", lambda self: [])

    class NoClassify:
        def classify(self, listings):
            assert listings == []  # pending은 분류 대상이 아님
            return {}

    nxt = today + timedelta(days=1)
    s.max_items = 5
    r2 = run(s, http, state, nxt, ["wevity", "linkareer"], use_llm=False, classifier=NoClassify())
    assert {c.key for c in r2.contests} == {"wevity:900002", "linkareer:800001"} and r2.deferred == 0
    save_marks(state, r2, nxt, issue=2)
    assert state.items["wevity:900002"]["status"] == "sent" and "listing" not in state.items["wevity:900002"]
    assert state.items["wevity:900002"]["first_seen"] == today.isoformat()  # 최초 발견일 유지
    assert state.pending(True) == []  # 비IT 건은 Non-IT 대기열에 남아 있다


def test_expired_pending_is_dropped(today):
    from datetime import timedelta

    from contest_digest.models import Listing

    state = State()
    old = Listing("wevity", "1", "AI 대회", "u/1", apply_end=today - timedelta(days=1))
    state.mark(old.key, today, old.apply_end, None, title=old.title, is_it=True, status="pending", listing=old)
    assert state.pending()[0].apply_end == old.apply_end


def test_pipeline_with_classifier_marks_non_it(http, today, tmp_path):
    from contest_digest.classify import Verdict
    from contest_digest.pipeline import save_marks

    class FakeClassifier:
        def classify(self, listings):
            # 키워드 규칙과 달리 900002(스마트시티)를 비IT로, 800002(시 낭송)도 비IT로
            return {it.key: Verdict(it.key in {"wevity:900001", "linkareer:800001"}, "", "fake") for it in listings}

    s = Settings()
    state = State()
    result = run(s, http, state, today, ["wevity", "linkareer"], use_llm=False, classifier=FakeClassifier())
    assert {c.key for c in result.contests} == {"wevity:900001", "linkareer:800001"}
    save_marks(state, result, today, issue=7)
    assert state.items["wevity:900001"]["issue"] == 7 and state.items["wevity:900001"]["it"] is True
    # 비IT는 Non-IT 트랙 대기열로
    assert {it.key for it in state.pending(False)} == {"wevity:900002", "wevity:900003", "linkareer:800002"}
    assert state.items["wevity:900003"]["title"] == "우리 동네 슬로건 공모전"

    non_it = process(s, http, state, today, "non-it", use_llm=False)
    # 800002는 상세 fixture가 없어 상세 수집 실패 → 대기열에 남는다
    assert {c.key for c in non_it.contests} == {"wevity:900002", "wevity:900003"} and non_it.deferred == 1
    assert non_it.title.endswith("Non-IT 공모전") and "IT 외 분야" in non_it.body
    save_marks(state, non_it, today, issue=3)
    assert state.items["wevity:900003"]["it"] is False and state.items["wevity:900003"]["issue"] == 3
    assert [it.key for it in state.pending()] == ["linkareer:800002"]
    # 다음 실행에서는 모두 기처리
    again = run(s, http, state, today, ["wevity", "linkareer"], use_llm=False, classifier=FakeClassifier())
    assert again.contests == [] and again.marks == []


def test_it_track_moves_non_it_category(http, today):
    from contest_digest.pipeline import save_marks

    class FakeExtractor:
        def extract(self, detail, today):
            from contest_digest.extract import metadata_only

            c = metadata_only(detail)
            c.category = "디자인/영상" if detail.listing.key == "wevity:900002" else "AI/데이터"
            return c

    from contest_digest.pipeline import classify_new

    s = Settings()
    state = State()
    classify_new(s, http, state, today, ["wevity", "linkareer"], use_llm=False)
    r = process(s, http, state, today, "it", use_llm=False, extractor=FakeExtractor())
    assert "wevity:900002" not in {c.key for c in r.contests} and r.moved == 1
    save_marks(state, r, today, issue=1)
    assert "wevity:900002" in {it.key for it in state.pending(False)}


def test_render_other_date_period(today):
    from contest_digest.models import OtherDate

    c = C("wevity:1", "2026-11-20", other_dates=[OtherDate(label="본선", date="2026-11-05", end_date="2026-11-06"),
                                                 OtherDate(label="발표", date="2026-12-01")])
    assert "본선 2026-11-05 \\~ 2026-11-06, 발표 2026-12-01" in render([c], today)


def test_title_week_of_month():
    from datetime import date

    from contest_digest.render import title, week_of_month

    assert title([], date(2026, 10, 12)) == "주간 공모전 - 10월 3째주 IT·AI 공모전"
    assert title([], date(2026, 10, 12), "non-it") == "주간 공모전 - 10월 3째주 Non-IT 공모전"
    assert week_of_month(date(2026, 10, 1)) == (10, 1)   # 목요일
    assert week_of_month(date(2026, 10, 26)) == (10, 5)
    assert week_of_month(date(2026, 11, 30)) == (12, 1)  # 그 주 목요일이 12월
    assert week_of_month(date(2026, 6, 1)) == (6, 1)


def test_render_escapes_tilde_to_prevent_strikethrough(today):
    body = render([C("wevity:1", "2026-11-20", eligibility="아동(6~12세), 청소년(13~17세)")], today)
    assert "6\\~12세" in body and "13\\~17세" in body
    assert "6~12" not in body


def test_non_it_track_drops_non_competitions(http, today):
    from contest_digest.extract import metadata_only
    from contest_digest.models import Listing

    class FakeExtractor:
        def extract(self, detail, today):
            c = metadata_only(detail)
            c.is_competition = detail.listing.key != "wevity:900003"
            return c

    state = State()
    sup = Listing("wevity", "9", "2026 청년 서포터즈 3기 모집", "u/9", apply_end=today)
    for it in [sup, Listing("wevity", "900003", "우리 동네 슬로건 공모전", "https://www.wevity.com/?c=find&s=1&gbn=view&ix=900003"),
               Listing("wevity", "900002", "스마트시티 아이디어", "https://www.wevity.com/?c=find&s=1&gbn=view&ix=900002")]:
        state.mark(it.key, today, it.apply_end, None, title=it.title, is_it=False, status="pending", listing=it)
    r = process(Settings(), http, state, today, "non-it", use_llm=False, extractor=FakeExtractor())
    assert [c.key for c in r.contests] == ["wevity:900002"]
    assert {it.key for it in r.excluded} == {"wevity:9", "wevity:900003"}
    toggle = r.body[r.body.index("<details>"):]
    assert "공모전이 아닌 모집 2건" in toggle and "서포터즈 3기" in toggle and "슬로건" in toggle
    assert r.body.index("스마트시티") < r.body.index("<details>") < r.body.index("---")

    # IT 트랙에도 같은 기준 적용
    it_sup = Listing("wevity", "8", "AI 서포터즈 모집", "u/8", apply_end=today)
    state.mark(it_sup.key, today, today, None, title=it_sup.title, is_it=True, status="pending", listing=it_sup)
    r_it = process(Settings(), http, state, today, "it", use_llm=False)
    assert r_it.contests == [] and [it.key for it in r_it.excluded] == ["wevity:8"]
    assert {m.listing.key: m.status for m in r.marks} == {"wevity:9": "skipped", "wevity:900003": "skipped",
                                                          "wevity:900002": "sent"}


def _pending_non_it(state, today, *keys):
    from contest_digest.models import Listing

    for k in keys:
        it = Listing("wevity", k, f"제목 {k}", f"https://www.wevity.com/?c=find&s=1&gbn=view&ix={k}")
        state.mark(it.key, today, None, None, title=it.title, is_it=False, status="pending", listing=it)


def test_non_it_track_promotes_once_when_detail_says_it(http, today):
    from contest_digest.extract import metadata_only
    from contest_digest.pipeline import save_marks

    class FakeExtractor:
        def extract(self, detail, today):
            c = metadata_only(detail)
            k = detail.listing.key
            # 900001: 상세상 AI 과제 해결형 → IT로 이동 / 900002: 이름에만 AI(명칭 공모) → 이동 안 함
            c.is_it, c.category = {"wevity:900001": (True, "AI/데이터"),
                                   "wevity:900002": (True, "기획/마케팅")}.get(k, (False, "디자인/영상"))
            return c

    state = State()
    _pending_non_it(state, today, "900001", "900002")
    r = process(Settings(), http, state, today, "non-it", use_llm=False, extractor=FakeExtractor())
    assert [c.key for c in r.contests] == ["wevity:900002"] and r.moved == 1
    save_marks(state, r, today, issue=2)
    assert [it.key for it in state.pending(True)] == ["wevity:900001"]
    assert state.was_moved("wevity:900001")

    # IT 트랙에서 상세가 비IT로 보여도 이미 한 번 옮긴 건은 되돌리지 않는다
    class NonItExtractor:
        def extract(self, detail, today):
            c = metadata_only(detail)
            c.category = "디자인/영상"
            return c

    r2 = process(Settings(), http, state, today, "it", use_llm=False, extractor=NonItExtractor())
    assert [c.key for c in r2.contests] == ["wevity:900001"] and r2.moved == 0


def test_keyword_rule_knows_ax():
    from contest_digest.filters import is_it_related
    from contest_digest.models import Listing

    assert is_it_related(Listing("linkareer", "1", "LOTTE AX CHALLENGE 2026", "u"))
    assert not is_it_related(Listing("linkareer", "2", "MAX 사진 공모전", "u"))
