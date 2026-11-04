from datetime import date, timedelta

from contest_digest.filters import is_it_related, select_new
from contest_digest.models import Listing
from contest_digest.state import State


def L(id, title, cats=(), end=None, source="wevity"):
    return Listing(source=source, id=id, title=title, url=f"u/{id}", apply_end=end, categories=list(cats))


def test_it_keywords():
    assert is_it_related(L("1", "AI공모전 2026"))
    assert is_it_related(L("2", "공공 데이터 활용 경진대회"))
    assert not is_it_related(L("3", "International Short Film Competition"))  # 'it' 부분 문자열 오탐 방지
    assert not is_it_related(L("4", "슬로건 공모", ["기획/아이디어", "웹/모바일/IT"]))
    assert is_it_related(L("5", "스마트시티 서비스 공모", ["웹/모바일/IT", "게임/소프트웨어"]))
    assert not is_it_related(L("6", "무언가 공모", ["기타"]))


def test_select_new(today):
    items = [
        L("1", "AI 해커톤", end=today + timedelta(days=9)),
        L("2", "AI 해커톤 지난것", end=today - timedelta(days=1)),
        L("3", "데이터 분석", end=today + timedelta(days=2)),
        L("4", "앱 개발", end=None),
        L("1", "AI 해커톤 중복", end=today),
    ]
    got = select_new(items, seen={"wevity:4"}, today=today)
    assert [g.id for g in got] == ["3", "1"]  # 마감순, 마감·기처리·중복 제외


def test_state_roundtrip_and_prune(tmp_path, today):
    path = tmp_path / "state.json"
    s = State()
    s.mark("wevity:1", today, today + timedelta(days=3), 10)
    s.mark("wevity:old", today - timedelta(days=200), date(2026, 1, 1), 1)
    s.mark("linkareer:x", today - timedelta(days=200), None, None)
    s.prune(today, 90)
    s.save(path, today)
    loaded = State.load(path)
    assert loaded.seen() == {"wevity:1"}
    assert loaded.last_run == today.isoformat()
    assert loaded.items["wevity:1"]["issue"] == 10


def test_state_missing_file(tmp_path):
    assert State.load(tmp_path / "nope.json").seen() == set()


def test_cross_source_title_dedupe(today):
    from contest_digest.filters import Fingerprint, title_keys

    a = L("1", "2026 DATA·AI 분석 경진대회", end=today, source="wevity")
    b = L("2", "[KISTI] 2026 DATA·AI 분석 경진대회", end=today, source="linkareer")
    c = L("3", "2026 데이터 분석 경진대회", end=today)
    assert title_keys(a.title) & title_keys(b.title)
    got = select_new([a, b, c], set(), today)
    assert len(got) == 2 and {g.title for g in got} >= {"2026 데이터 분석 경진대회"}
    # 지난 실행에서 보낸 제목도 제외
    assert select_new([b], set(), today, history=[Fingerprint.of(a.title, source="wevity")]) == []
    # 부제 표기만 다른 경우 (실제 사례)
    w = L("4", "[CJ제일제당] 햇반 30주년 AI 공모전 (갓 지은 AI디어)", end=today, source="wevity")
    k = L("5", "[CJ제일제당] 햇반 30주년 AI 공모전 ‘갓 지은 AI디어’", end=today, source="linkareer")
    assert len(select_new([w, k], set(), today)) == 1


def test_cross_source_fuzzy_dedupe(today):
    """제목 표기가 다른 같은 공모전을 주최·마감까지 종합해 찾는다 (실제 사례)."""
    from contest_digest.filters import Fingerprint, is_same_contest

    def fp(title, host=None, end=today, source="wevity"):
        return Fingerprint.of(title, host, end, source)

    # 꼬리말 '공모' vs '공모전'
    assert is_same_contest(fp("[국민체육진흥공단] 체육시설 안전·경영 우수사례 공모전", "국민체육진흥공단"),
                           fp("[국민체육진흥공단] 체육시설 안전·경영 우수사례 공모", "국민체육진흥공단", source="linkareer"))
    # 띄어쓰기·단어 생략·주최 표기 차이
    assert is_same_contest(fp("2026 칠곡군청맛길 숏폼 공모전", "칠곡군"),
                           fp("[칠곡군청맛길] 2026 칠곡 군청맛길 숏폼 영상 공모전", "칠곡군청맛길", source="linkareer"))
    # 같은 주최의 다른 공모전은 별개
    assert not is_same_contest(fp("2026년 체육관련 규제개선 대국민 아이디어 공모전", "국민체육진흥공단", today + timedelta(days=16)),
                               fp("[국민체육진흥공단] 체육시설 안전·경영 우수사례 공모", "국민체육진흥공단", source="linkareer"))
    # 같은 소스라도 마감·내용이 같으면 재등록으로 본다
    assert is_same_contest(fp("2026 「다시, 다리다」 유휴공간 활성화 아이디어 공모전", "문화정원", source="linkareer"),
                           fp("[문화정원] 2026 (다시, 다리다) 유휴공간 활성화 아이디어 공모전", "문화정원", source="linkareer"))
    assert is_same_contest(fp("2027 KSPO 스포츠가치센터 슬로건 공모전", "KSPO스포츠가치센터", source="linkareer"),
                           fp("스포츠가치센터 슬로건 공모전", "국민체육진흥공단 스츠가치센터", source="linkareer"))
    # 어순만 바뀐 제목
    assert is_same_contest(fp("KISA에게 바란다, 국민이 함께하는 '모두의 KISA'", "한국인터넷진흥원", source="linkareer"),
                           fp("모두의 KISA 공모 - KISA에게 바란다 모두의 KISA", "한국인터넷진흥원"))
    # 같은 소스에서 마감이 다르면 별개
    assert not is_same_contest(fp("칠곡 숏폼 공모전"), fp("칠곡 숏폼 영상 공모전", end=today + timedelta(days=5)))
    # 같은 기관·같은 마감의 다른 프로그램은 별개 (주최명이 제목 대부분인 경우)
    assert not is_same_contest(fp("2026 농림식품기술기획평가원 iPET 서포터즈 5기 모집", "농림식품기술기획평가원"),
                               fp("2026 농림식품기술기획평가원 iPET 농플루언서 모집", "농림식품기술기획평가원"))
    # 마감이 크게 다르면 비슷한 제목이어도 별개 (회차 차이)
    assert not is_same_contest(fp("청년 정책 아이디어 공모전", "A재단"),
                               fp("청년 정책 아이디어 공모", "B협회", today + timedelta(days=60), "linkareer"))
