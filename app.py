# app.py — MT 실시간 의견·투표 대시보드
# 실행: pip install streamlit streamlit-autorefresh && streamlit run app.py

import sqlite3
import threading
from contextlib import closing
from datetime import datetime
import secrets
import streamlit as st

# =========================
# 기본 설정
# =========================
st.set_page_config(page_title="MT 실시간 의견/투표 대시보드", page_icon="🗳️", layout="wide")
st.title("🗳️ MT 의견·투표 대시보드")
st.caption("음식 1조 투표/대시보드 입니다!")

# 자동 새로고침(선택)
REFRESH_MS = 2000
try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=REFRESH_MS, key="auto_refresh")
except Exception:
    pass

# 세션별 익명 사용자 ID 생성(로그인 없이 1인 1표 보장용)
if "user_id" not in st.session_state:
    st.session_state["user_id"] = secrets.token_urlsafe(16)
USER_ID = st.session_state["user_id"]

# =========================
# DB 초기화 (SQLite + WAL)
# =========================
DB_PATH = "mt_dashboard.db"

@st.cache_resource
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    with closing(conn.cursor()) as cur:
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        # 의견 테이블
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL CHECK(category IN ('food','festival')),
                content  TEXT NOT NULL,
                votes    INTEGER NOT NULL DEFAULT 0, -- (과거 호환용, 집계에는 미사용)
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ideas_cat ON ideas(category);")
        # 투표 테이블 (한 사용자/한 의견 1표)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idea_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(idea_id, user_id),
                FOREIGN KEY (idea_id) REFERENCES ideas(id) ON DELETE CASCADE
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_votes_idea ON votes(idea_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_votes_user ON votes(user_id);")
    return conn

@st.cache_resource
def get_lock():
    return threading.Lock()

conn = get_conn()
lock = get_lock()

# =========================
# DB 유틸 함수
# =========================
def add_idea(category: str, content: str):
    content = (content or "").strip()
    if not content:
        return False
    with lock, closing(conn.cursor()) as cur:
        cur.execute("INSERT INTO ideas(category, content, votes) VALUES (?, ?, 0)", (category, content))
    return True

def has_voted(idea_id: int, user_id: str) -> bool:
    with closing(conn.cursor()) as cur:
        cur.execute("SELECT 1 FROM votes WHERE idea_id = ? AND user_id = ? LIMIT 1", (idea_id, user_id))
        return cur.fetchone() is not None

def toggle_vote(idea_id: int, user_id: str):
    """이미 투표했으면 취소(삭제), 아니면 투표(추가)."""
    with lock, closing(conn.cursor()) as cur:
        cur.execute("SELECT id FROM votes WHERE idea_id = ? AND user_id = ? LIMIT 1", (idea_id, user_id))
        row = cur.fetchone()
        if row:
            # 취소
            cur.execute("DELETE FROM votes WHERE id = ?", (row[0],))
        else:
            # 투표 (중복 방지는 UNIQUE 제약이 처리)
            try:
                cur.execute("INSERT INTO votes(idea_id, user_id) VALUES (?, ?)", (idea_id, user_id))
            except sqlite3.IntegrityError:
                # 경합 중 중복 삽입이면 무시
                pass

def delete_idea(idea_id: int):
    with lock, closing(conn.cursor()) as cur:
        # 외래키 ON DELETE CASCADE가 votes 함께 삭제
        cur.execute("DELETE FROM ideas WHERE id = ?", (idea_id,))

def fetch_ideas(category: str, user_id: str):
    """표시용으로 votes 테이블에서 집계한 최신 투표수를 사용."""
    with closing(conn.cursor()) as cur:
        cur.execute(
            """
            SELECT i.id,
                   i.content,
                   COALESCE(v.cnt, 0) AS votes,
                   i.created_at,
                   CASE WHEN my.myvote IS NULL THEN 0 ELSE 1 END AS i_voted
              FROM ideas i
              LEFT JOIN (
                    SELECT idea_id, COUNT(*) AS cnt
                      FROM votes
                     GROUP BY idea_id
              ) v ON v.idea_id = i.id
              LEFT JOIN (
                    SELECT idea_id, 1 AS myvote
                      FROM votes
                     WHERE user_id = ?
              ) my ON my.idea_id = i.id
             WHERE i.category = ?
             ORDER BY votes DESC, datetime(i.created_at) ASC, i.id ASC
            """,
            (user_id, category),
        )
        rows = cur.fetchall()
    return [
        {"id": r[0], "content": r[1], "votes": int(r[2] or 0), "created_at": r[3], "i_voted": bool(r[4])}
        for r in rows
    ]

# =========================
# 공통 렌더링 함수
# =========================
CATEGORY_LABELS = {"food": "🍽️ 음식 메뉴 정하기", "festival": "🎉 축제 활동 정하기"}

SECTION_HELP = {
    "food": "정해진 사항은 케이크에 보석을 숨기는 것 이지만, 자유롭게 의견 정해주세요!",
    "festival": "노래를 할지, 릴레이 댄스를 하고싶은지 하고싶은 것들을 작성해 주세요!",
}

PLACEHOLDERS = {
            "food": "예) 메인은 픽스하고, 저희끼리 파전 해먹어요!",
            "festival": "예) 소방차 노래로 릴레이 댄스해요!",
}    

def render_category(category_code: str):
    label = CATEGORY_LABELS[category_code]
    with st.container():
        st.subheader(label)
        st.caption(SECTION_HELP[category_code])

        # ---- 의견 추가 폼 ----
        def form_ctx(key: str):
            try:
                return st.form(key=key, clear_on_submit=True)
            except TypeError:
                return st.form(key=key)

        with form_ctx(f"form_add_{category_code}"):
            new_text = st.text_input(
                "의견 입력",
                key=f"input_{category_code}",
                placeholder=PLACEHOLDERS.get(category_code, "의견을 입력하세요"),
                label_visibility="collapsed",
            )
            submitted = st.form_submit_button("추가", use_container_width=True)
            if submitted:
                ok = add_idea(category_code, new_text)
                if ok:
                    st.toast("의견이 추가되었습니다 ✨")
                    try:
                        st.session_state[f"input_{category_code}"] = ""
                    except Exception:
                        pass
                    st.rerun()
                else:
                    st.warning("내용을 입력해주세요.")

        st.divider()

        # ---- 목록 & 액션 ----
        ideas = fetch_ideas(category_code, USER_ID)
        if not ideas:
            st.info("아직 등록된 의견이 없습니다. 위 입력창에서 첫 의견을 남겨보세요!")
        else:
            for idea in ideas:
                cols = st.columns([8, 1.4, 1.2, 1.2])
                with cols[0]:
                    st.markdown(f"**{idea['content']}**")
                    st.caption(datetime.fromisoformat(idea["created_at"]).strftime("%Y-%m-%d %H:%M"))
                with cols[1]:
                    st.markdown(f"현재 투표수: **{idea['votes']}**")
                with cols[2]:
                    btn_label = "❌" if idea["i_voted"] else "👍️"
                    if st.button(btn_label, key=f"vote_{category_code}_{idea['id']}"):
                        toggle_vote(idea["id"], USER_ID)
                        st.rerun()
                with cols[3]:
                    if st.button("🗑️", key=f"del_{category_code}_{idea['id']}"):
                        delete_idea(idea["id"])
                        st.rerun()

# =========================
# 레이아웃: 2열 (왼: 음식 / 오른: 축제)
# =========================
left, right = st.columns(2)
with left:
    render_category("food")
with right:
    render_category("festival")



st.markdown(
    """
    <br>
    <div style='font-size: 1rem; opacity: 0.7;'>
        아 의견을 못 정하겠다고요? 사다리게임 드가자
        🔗 <a href="https://search.naver.com/search.naver?where=nexearch&query=%EB%84%A4%EC%9D%B4%EB%B2%84+%EC%82%AC%EB%8B%A4%EB%A6%AC&ie=utf8&sm=tab_she&qdt=0" target="_blank">사다리 타기 바로가기</a>
    </div>
    """,
    unsafe_allow_html=True,
)