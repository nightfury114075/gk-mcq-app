import streamlit as st
import psycopg2
from psycopg2 import pool as pg_pool
import pandas as pd
import random
import math
import time
from datetime import date, timedelta
from contextlib import contextmanager

# -----------------------------------------------------------------------------
# 0. PAGE CONFIG (must be first Streamlit call)
# -----------------------------------------------------------------------------
st.set_page_config(page_title="GK Exam Engine", page_icon="📚", layout="wide", initial_sidebar_state="expanded")

# -----------------------------------------------------------------------------
# 1. DATABASE LAYER (Connection Pool + Auto-Reconnect, thread-safe for multi-session)
# -----------------------------------------------------------------------------
@st.cache_resource
def get_pool():
    try:
        return pg_pool.ThreadedConnectionPool(
            1, 15,
            host=st.secrets["DB_HOST"],
            database=st.secrets["DB_NAME"],
            user=st.secrets["DB_USER"],
            password=st.secrets["DB_PASSWORD"],
            port=st.secrets.get("DB_PORT", "5432"),
            connect_timeout=10,
        )
    except Exception as e:
        st.error(f"ডেটাবেজ সংযোগ ব্যর্থ: {e}")
        st.stop()

db_pool = get_pool()

def _ensure_alive(conn):
    """Ping a pooled connection; transparently swap in a fresh one if it's dead."""
    try:
        with conn.cursor() as c:
            c.execute("SELECT 1")
        return conn
    except Exception:
        try:
            db_pool.putconn(conn, close=True)
        except Exception:
            pass
        return db_pool.getconn()

@contextmanager
def get_cursor(commit=False):
    conn = db_pool.getconn()
    conn = _ensure_alive(conn)
    try:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)

def fetch_df(query, params=None):
    with get_cursor() as cur:
        cur.execute(query, params or ())
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)

# -----------------------------------------------------------------------------
# 2. DATA ACCESS (cached + batched + paginated to minimize round-trips and payload)
# -----------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def get_categories():
    with get_cursor() as cur:
        cur.execute("SELECT category_id, category_name FROM categories ORDER BY category_name;")
        return cur.fetchall()

@st.cache_data(ttl=300, show_spinner=False)
def get_total_question_count():
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM questions;")
        return cur.fetchone()[0]

@st.cache_data(ttl=300, show_spinner=False)
def get_category_distribution():
    query = """
        SELECT c.category_name, COUNT(q.question_id) AS q_count
        FROM categories c
        LEFT JOIN questions q ON q.category_id = c.category_id
        GROUP BY c.category_name
        ORDER BY q_count DESC;
    """
    return fetch_df(query)

def fetch_question_bank(question_ids):
    """One round-trip fetch of every question + option needed for a given set of IDs."""
    if not question_ids:
        return {}
    with get_cursor() as cur:
        cur.execute(
            "SELECT question_id, question_text, explanation, source_url FROM questions WHERE question_id = ANY(%s);",
            (question_ids,),
        )
        q_rows = cur.fetchall()
        cur.execute(
            "SELECT question_id, option_text, is_correct FROM options WHERE question_id = ANY(%s);",
            (question_ids,),
        )
        o_rows = cur.fetchall()

    bank = {qid: {"text": text, "explain": explain, "source_url": source_url, "options": []} for qid, text, explain, source_url in q_rows}
    for qid, opt_text, is_correct in o_rows:
        if qid in bank:
            bank[qid]["options"].append((opt_text, is_correct))
    for qid in bank:
        random.shuffle(bank[qid]["options"])
    return bank

@st.cache_data(ttl=180, show_spinner=False)
def get_study_page(category_id, search_term, page, page_size):
    """
    True server-side pagination for Study Mode: only the IDs for the requested
    page are ever pulled, then options are batch-fetched for just those IDs.
    This keeps Study Mode fast even as the question bank grows into the
    thousands, instead of loading a whole category into memory per keystroke.
    """
    conditions, params = [], []
    if category_id is not None:
        conditions.append("category_id = %s")
        params.append(category_id)
    if search_term:
        conditions.append("question_text ILIKE %s")
        params.append(f"%{search_term}%")

    base = "SELECT question_id FROM questions"
    if conditions:
        base += " WHERE " + " AND ".join(conditions)

    with get_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM ({base}) t;", params)
        total = cur.fetchone()[0]

        paged = base + " ORDER BY question_id LIMIT %s OFFSET %s;"
        cur.execute(paged, params + [page_size, (page - 1) * page_size])
        ids = [r[0] for r in cur.fetchall()]

    bank = fetch_question_bank(ids)
    return ids, bank, total

def get_randomized_question_ids(category_id=None, limit=None):
    with get_cursor() as cur:
        if category_id:
            cur.execute("SELECT question_id FROM questions WHERE category_id = %s ORDER BY RANDOM();", (category_id,))
        else:
            cur.execute("SELECT question_id FROM questions ORDER BY RANDOM();")
        ids = [r[0] for r in cur.fetchall()]
    return ids[:limit] if limit else ids

def save_test_score(test_type, attempted, correct, percentage):
    with get_cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO exam_history (test_type, total_attempted, correct_answers, score_percentage)
               VALUES (%s, %s, %s, %s)""",
            (test_type, attempted, correct, percentage),
        )
    get_history.clear()

@st.cache_data(ttl=30, show_spinner=False)
def get_history():
    query = "SELECT test_date, test_type, total_attempted, correct_answers, score_percentage FROM exam_history ORDER BY test_date ASC;"
    return fetch_df(query)

def compute_streak(history_df):
    if history_df.empty:
        return 0
    test_dates = sorted(set(pd.to_datetime(history_df["test_date"]).dt.date), reverse=True)
    today = date.today()
    if test_dates[0] not in (today, today - timedelta(days=1)):
        return 0
    streak = 1
    expected = test_dates[0] - timedelta(days=1)
    for d in test_dates[1:]:
        if d == expected:
            streak += 1
            expected -= timedelta(days=1)
        else:
            break
    return streak

# -----------------------------------------------------------------------------
# 3. APPLICATION STATE
# -----------------------------------------------------------------------------
def init_state():
    defaults = {
        "nav_mode": "🏠 হোম (Dashboard)",
        "test_active": False,
        "question_queue": [],
        "question_bank": {},
        "current_q_index": 0,
        "end_timestamp": None,
        "total_duration_seconds": None,
        "test_type_label": "",
        "current_q_id": None,
        "current_q_text": None,
        "current_q_explain": None,
        "current_q_source_url": None,
        "current_options": None,
        "total_attempted": 0,
        "correct_count": 0,
        "session_history": [],
        "study_page": 1,
        "study_page_size": 20,
        "study_search_term": "",
        "study_last_cat": None,
        "time_is_up": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()

def load_current_mcq():
    idx = st.session_state.current_q_index
    if idx < len(st.session_state.question_queue):
        qid = st.session_state.question_queue[idx]
        data = st.session_state.question_bank.get(qid)
        if data:
            st.session_state.current_q_id = qid
            st.session_state.current_q_text = data["text"]
            st.session_state.current_q_explain = data["explain"]
            st.session_state.current_q_source_url = data.get("source_url")
            st.session_state.current_options = data["options"]
            return
    st.session_state.current_q_id = None

def reset_test_state():
    st.session_state.test_active = False
    st.session_state.question_queue = []
    st.session_state.question_bank = {}
    st.session_state.current_q_index = 0
    st.session_state.end_timestamp = None
    st.session_state.total_duration_seconds = None
    st.session_state.total_attempted = 0
    st.session_state.correct_count = 0
    st.session_state.session_history = []
    st.session_state.current_q_id = None
    st.session_state.time_is_up = False

def start_test(category_id, q_limit, t_limit, label):
    all_ids = get_randomized_question_ids(category_id, limit=q_limit)
    if not all_ids:
        st.error("দুঃখিত, এই সেকশনে কোনো প্রশ্ন পাওয়া যায়নি।")
        return False
    st.session_state.question_bank = fetch_question_bank(all_ids)
    st.session_state.test_active = True
    st.session_state.test_type_label = label
    st.session_state.question_queue = all_ids
    st.session_state.current_q_index = 0
    st.session_state.total_duration_seconds = t_limit * 60 if t_limit > 0 else None
    st.session_state.end_timestamp = time.time() + t_limit * 60 if t_limit > 0 else None
    st.session_state.time_is_up = False
    load_current_mcq()
    return True

# -----------------------------------------------------------------------------
# 4. GLOBAL STYLING
# -----------------------------------------------------------------------------
def inject_css():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Hind+Siliguri:wght@400;500;600;700&family=Inter:wght@400;500;600;700;800&display=swap');

    html, body, [class*="css"] { font-family: 'Hind Siliguri', 'Inter', 'Segoe UI', sans-serif !important; }

    :root {
        --accent-1: #6366f1;
        --accent-2: #8b5cf6;
        --accent-3: #ec4899;
        --ok: #10b981;
        --warn: #f59e0b;
        --danger: #ef4444;
    }

    @keyframes fadeIn { from {opacity:0;} to {opacity:1;} }
    @keyframes slideIn { from {opacity:0; transform: translateY(14px);} to {opacity:1; transform: translateY(0);} }
    @keyframes popIn { from {opacity:0; transform: scale(.92);} to {opacity:1; transform: scale(1);} }
    @keyframes floatUp { 0%{transform:translateY(0);} 50%{transform:translateY(-4px);} 100%{transform:translateY(0);} }

    #MainMenu, footer { visibility: hidden; }

    /* ---------- Hero header ---------- */
    .app-hero {
        background: linear-gradient(120deg, var(--accent-1) 0%, var(--accent-2) 55%, var(--accent-3) 100%);
        padding: 30px 34px; border-radius: 22px; color: #fff !important; margin-bottom: 24px;
        box-shadow: 0 14px 34px rgba(99,102,241,0.28); animation: fadeIn .45s ease;
        position: relative; overflow: hidden;
    }
    .app-hero::after {
        content: ""; position: absolute; right: -40px; top: -60px; width: 220px; height: 220px;
        background: rgba(255,255,255,0.10); border-radius: 50%;
    }
    .app-hero h1 { margin: 0 0 4px 0; color: #fff !important; font-size: 26px; font-weight: 800; }
    .app-hero p { margin: 0; color: rgba(255,255,255,0.88) !important; font-size: 14.5px; }

    /* ---------- Stat / metric cards ---------- */
    .stat-card {
        background: var(--secondary-background-color); border-radius: 16px; padding: 16px 18px;
        border: 1px solid rgba(128,128,128,0.16); box-shadow: 0 4px 14px rgba(0,0,0,0.06);
        animation: slideIn .35s ease-out;
    }
    .stat-card .label { font-size: 12.5px; opacity: 0.65; font-weight: 600; letter-spacing: .2px; }
    .stat-card .value { font-size: 26px; font-weight: 800; margin-top: 2px; background: linear-gradient(120deg, var(--accent-1), var(--accent-3)); -webkit-background-clip: text; background-clip: text; color: transparent; }

    /* ---------- Question / content cards ---------- */
    .question-card {
        background: var(--secondary-background-color); color: var(--text-color);
        border-radius: 18px; padding: 22px 24px; box-shadow: 0 8px 26px rgba(0,0,0,0.10);
        border-left: 6px solid var(--accent-2); animation: slideIn .3s ease-out; margin-bottom: 18px;
    }
    .big-question { font-size: 21px; font-weight: 700; line-height: 1.6; color: var(--text-color); }
    .study-q { font-size: 17.5px; font-weight: 600; color: var(--text-color); margin-bottom: 10px; }

    .opt-correct {
        background: rgba(16, 185, 129, 0.14); color: #0d9c6d;
        border: 1.5px solid var(--ok); border-radius: 12px;
        padding: 9px 14px; margin-bottom: 8px; font-weight: 600; animation: popIn .3s ease;
    }
    .opt-normal {
        background: var(--background-color); color: var(--text-color);
        border: 1.5px solid rgba(128,128,128,0.22); border-radius: 12px;
        padding: 9px 14px; margin-bottom: 8px;
    }

    div[role="radiogroup"] label {
        background: var(--secondary-background-color);
        border: 1.8px solid rgba(128,128,128,0.22); border-radius: 12px;
        padding: 11px 16px; margin-bottom: 9px; transition: all .18s ease;
    }
    div[role="radiogroup"] label:hover {
        border-color: var(--accent-2); background: rgba(139,92,246,0.08); transform: translateX(3px);
    }

    .stButton>button {
        border-radius: 11px; font-weight: 600; transition: all .18s ease; border: none;
    }
    .stButton>button:hover { transform: translateY(-2px); box-shadow: 0 10px 20px rgba(99,102,241,0.28); }
    .stButton>button[kind="primary"] {
        background: linear-gradient(120deg, var(--accent-1), var(--accent-2)) !important;
    }

    [data-testid="stMetric"] {
        background: var(--secondary-background-color); border-radius: 14px; padding: 12px 16px;
        border: 1px solid rgba(128,128,128,0.18);
    }

    .source-link-icon { text-decoration: none !important; margin-left: 8px; font-size: 15px; }

    /* ---------- Sidebar nav pill styling ---------- */
    section[data-testid="stSidebar"] div[role="radiogroup"] label {
        background: transparent; border: none; border-radius: 10px; padding: 9px 12px;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] label:hover {
        background: rgba(139,92,246,0.12); transform: none;
    }

    /* ---------- Countdown ring ---------- */
    .timer-wrap { display: flex; justify-content: flex-end; margin-bottom: 8px; }
    .timer-ring {
        width: 78px; height: 78px; border-radius: 50%;
        display: flex; align-items: center; justify-content: center;
        font-weight: 800; font-size: 15px; color: var(--text-color);
        box-shadow: 0 6px 16px rgba(0,0,0,0.12);
        animation: floatUp 2.4s ease-in-out infinite;
    }
    .timer-ring-inner {
        width: 62px; height: 62px; border-radius: 50%; background: var(--secondary-background-color);
        display: flex; align-items: center; justify-content: center;
    }

    /* ---------- Category chip (dashboard) ---------- */
    .quick-card {
        background: var(--secondary-background-color); border-radius: 16px; padding: 18px;
        border: 1px solid rgba(128,128,128,0.16); box-shadow: 0 4px 14px rgba(0,0,0,0.06);
        height: 100%;
    }
    .quick-card h4 { margin: 0 0 4px 0; }
    .quick-card p { margin: 0 0 12px 0; font-size: 13px; opacity: 0.75; }

    /* ---------- Mobile responsiveness ---------- */
    @media (max-width: 640px) {
        .app-hero { padding: 20px 20px; border-radius: 16px; }
        .app-hero h1 { font-size: 21px; }
        .question-card { padding: 16px 16px; border-radius: 14px; }
        .big-question { font-size: 17.5px; }
        .study-q { font-size: 15.5px; }
        div[role="radiogroup"] label { padding: 9px 12px; }
    }
    </style>
    """, unsafe_allow_html=True)

inject_css()

# -----------------------------------------------------------------------------
# 5. TIMER — Native Streamlit Fragment, rendered as a donut-ring countdown
# -----------------------------------------------------------------------------
@st.fragment(run_every=1)
def live_timer():
    if not (st.session_state.test_active and st.session_state.end_timestamp):
        return
    total = st.session_state.total_duration_seconds or 1
    time_left = st.session_state.end_timestamp - time.time()

    if time_left <= 0:
        if not st.session_state.time_is_up:
            st.session_state.time_is_up = True
            st.rerun()
        return

    pct_remaining = max(0.0, min(1.0, time_left / total))
    degrees = pct_remaining * 360
    mins, secs = divmod(int(time_left), 60)

    if pct_remaining > 0.5:
        color = "#10b981"
    elif pct_remaining > 0.15:
        color = "#f59e0b"
    else:
        color = "#ef4444"

    st.markdown(f"""
    <div class="timer-wrap">
        <div class="timer-ring" style="background: conic-gradient({color} {degrees}deg, rgba(128,128,128,0.18) 0deg);">
            <div class="timer-ring-inner" style="color:{color};">{mins:02d}:{secs:02d}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# 6. SIDEBAR NAVIGATION
# -----------------------------------------------------------------------------
NAV_OPTIONS = [
    "🏠 হোম (Dashboard)",
    "📖 পড়াশোনা (Study Mode)",
    "✍️ লাইভ পরীক্ষা (Live MCQ)",
    "📈 প্রগতি ও হিস্ট্রি (Progress)",
]

with st.sidebar:
    st.markdown("## 📚 GK Exam Engine")
    st.caption("BCS প্রস্তুতির জন্য স্মার্ট লার্নিং পোর্টাল")
    app_mode = st.radio("মোড নির্বাচন করুন:", NAV_OPTIONS, label_visibility="collapsed", key="nav_mode")
    st.markdown("---")
    if st.button("🔄 ডেটা রিফ্রেশ করুন", use_container_width=True):
        get_categories.clear()
        get_study_page.clear()
        get_history.clear()
        get_total_question_count.clear()
        get_category_distribution.clear()
        st.rerun()

categories = get_categories()
category_options = {"সবগুলো ক্যাটাগরি (All)": None}
for cid, name in categories:
    category_options[name] = cid

# -----------------------------------------------------------------------------
# 7. MODE 0 — DASHBOARD (overview + one-tap quick start)
# -----------------------------------------------------------------------------
if app_mode == "🏠 হোম (Dashboard)":
    st.markdown(
        '<div class="app-hero"><h1>👋 স্বাগতম!</h1>'
        '<p>আজকের প্রস্তুতি শুরু করুন — নিচে থেকে একটি কুইক-স্টার্ট বেছে নিন অথবা সরাসরি মোড পরিবর্তন করুন।</p></div>',
        unsafe_allow_html=True,
    )

    history_df = get_history()
    total_q = get_total_question_count()
    tests_taken = len(history_df)
    avg_score = history_df["score_percentage"].mean() if not history_df.empty else 0
    streak = compute_streak(history_df)

    c1, c2, c3, c4 = st.columns(4)
    for col, label, value in [
        (c1, "মোট প্রশ্ন", f"{total_q}"),
        (c2, "মোট পরীক্ষা", f"{tests_taken}"),
        (c3, "গড় স্কোর", f"{avg_score:.1f}%"),
        (c4, "টানা দিন (Streak)", f"{streak} 🔥"),
    ]:
        with col:
            st.markdown(f'<div class="stat-card"><div class="label">{label}</div><div class="value">{value}</div></div>', unsafe_allow_html=True)

    st.write("")
    st.markdown("#### ⚡ কুইক স্টার্ট")
    q1, q2, q3 = st.columns(3)
    quick_cards = [
        (q1, "🟢 কুইক প্র্যাকটিস", "১০টি র‍্যান্ডম প্রশ্ন, কোনো টাইমার নেই।", None, 10, 0, "কুইক প্র্যাকটিস (১০টি)"),
        (q2, "🟡 টাইমড স্প্রিন্ট", "২০টি প্রশ্ন, ১৫ মিনিটের মধ্যে।", None, 20, 15, "টাইমড স্প্রিন্ট (২০টি, ১৫ মিনিট)"),
        (q3, "🔴 ফুল মক টেস্ট", "সবগুলো প্রশ্ন, ৬০ মিনিটের মধ্যে।", None, 9999, 60, "ফুল মক টেস্ট (সম্মিলিত)"),
    ]
    for col, title, desc, cat_id, q_limit, t_limit, label in quick_cards:
        with col:
            st.markdown(f'<div class="quick-card"><h4>{title}</h4><p>{desc}</p></div>', unsafe_allow_html=True)
            if st.button("শুরু করুন", key=f"quick_{title}", use_container_width=True, type="primary"):
                if start_test(cat_id, q_limit, t_limit, label):
                    st.session_state.nav_mode = "✍️ লাইভ পরীক্ষা (Live MCQ)"
                    st.rerun()

    if not history_df.empty:
        st.write("")
        st.markdown("#### 📊 ক্যাটাগরি অনুযায়ী প্রশ্ন সংখ্যা")
        dist_df = get_category_distribution()
        st.bar_chart(dist_df.set_index("category_name")["q_count"])

# -----------------------------------------------------------------------------
# 8. MODE 1 — STUDY MODE (server-side pagination + debounced search)
# -----------------------------------------------------------------------------
elif app_mode == "📖 পড়াশোনা (Study Mode)":
    st.markdown('<div class="app-hero"><h1>📖 তথ্য ভাণ্ডার ও রিভিশন</h1><p>ক্যাটাগরি বেছে নিন, খুঁজুন, এবং পাতা অনুযায়ী পড়ুন।</p></div>', unsafe_allow_html=True)

    col_a, col_b = st.columns([2, 1])
    with col_a:
        selected_cat_name = st.selectbox("ক্যাটাগরি বেছে নিন:", list(category_options.keys()))
    with col_b:
        page_size = st.selectbox("প্রতি পাতায় প্রশ্ন:", [10, 20, 50], index=1, key="study_page_size")

    if selected_cat_name != st.session_state.study_last_cat:
        st.session_state.study_last_cat = selected_cat_name
        st.session_state.study_page = 1

    # Search wrapped in a form so DB queries fire on submit, not on every keystroke.
    with st.form("study_search_form", clear_on_submit=False):
        s_col, b_col = st.columns([4, 1])
        with s_col:
            search_input = st.text_input(
                "🔍 প্রশ্ন খুঁজুন", value=st.session_state.study_search_term,
                placeholder="কীওয়ার্ড লিখুন...", label_visibility="collapsed",
            )
        with b_col:
            submitted = st.form_submit_button("🔍 অনুসন্ধান", use_container_width=True)
    if submitted:
        st.session_state.study_search_term = search_input
        st.session_state.study_page = 1

    search_term = st.session_state.study_search_term
    cat_id = category_options[selected_cat_name]
    page = st.session_state.study_page

    ids, bank, total_found = get_study_page(cat_id, search_term, page, page_size)
    total_pages = max(1, math.ceil(total_found / page_size))
    page = min(page, total_pages)
    st.session_state.study_page = page

    st.caption(f"📊 মোট {total_found} টি প্রশ্ন পাওয়া গেছে — পাতা {page}/{total_pages}")

    if not ids:
        st.info("কোনো প্রশ্ন পাওয়া যায়নি। ভিন্ন কীওয়ার্ড বা ক্যাটাগরি চেষ্টা করুন।")
    else:
        for i, qid in enumerate(ids):
            item = bank.get(qid)
            if not item:
                continue
            with st.container(border=True):
                st.markdown(f'<div class="study-q">{(page - 1) * page_size + i + 1}. {item["text"]}</div>', unsafe_allow_html=True)
                cols = st.columns(2)
                for j, (opt_text, is_correct) in enumerate(item["options"]):
                    css_class = "opt-correct" if is_correct else "opt-normal"
                    icon = "✅" if is_correct else "⚪"
                    with cols[j % 2]:
                        st.markdown(f'<div class="{css_class}">{icon} {opt_text}</div>', unsafe_allow_html=True)
                with st.expander("💡 ব্যাখ্যা দেখুন"):
                    link_html = ""
                    if item.get("source_url"):
                        link_html = f' <a href="{item["source_url"]}" target="_blank" class="source-link-icon" title="সূত্র দেখুন">🔗</a>'
                    st.markdown(f'{item["explain"]}{link_html}', unsafe_allow_html=True)

        st.write("")
        p1, p2, p3, p4, p5 = st.columns([1, 1, 2, 1, 1])
        with p1:
            if st.button("⏮ প্রথম", disabled=(page <= 1), use_container_width=True):
                st.session_state.study_page = 1
                st.rerun()
        with p2:
            if st.button("◀ আগের", disabled=(page <= 1), use_container_width=True):
                st.session_state.study_page = page - 1
                st.rerun()
        with p3:
            st.markdown(f"<div style='text-align:center; padding-top:8px;'>পাতা {page} / {total_pages}</div>", unsafe_allow_html=True)
        with p4:
            if st.button("পরের ▶", disabled=(page >= total_pages), use_container_width=True):
                st.session_state.study_page = page + 1
                st.rerun()
        with p5:
            if st.button("শেষ ⏭", disabled=(page >= total_pages), use_container_width=True):
                st.session_state.study_page = total_pages
                st.rerun()

# -----------------------------------------------------------------------------
# 9. MODE 2 — LIVE MCQ TEST (batched question bank = zero per-question DB calls)
# -----------------------------------------------------------------------------
elif app_mode == "✍️ লাইভ পরীক্ষা (Live MCQ)":

    if not st.session_state.test_active:
        st.markdown('<div class="app-hero"><h1>⚙️ পরীক্ষার সেটিংস</h1><p>নিজের মতো করে পরীক্ষা সাজিয়ে নিন।</p></div>', unsafe_allow_html=True)
        with st.container(border=True):
            test_type = st.radio(
                "পরীক্ষার ধরন নির্বাচন করুন:",
                ["নির্দিষ্ট ক্যাটাগরি", "সম্মিলিত পরীক্ষা (সব মিলিয়ে)"],
                horizontal=True,
            )

            chosen_cat_id = None
            chosen_cat_name = None
            if test_type == "নির্দিষ্ট ক্যাটাগরি":
                chosen_cat_name = st.selectbox("ক্যাটাগরি বেছে নিন:", list(category_options.keys()))
                chosen_cat_id = category_options[chosen_cat_name]

            col_q, col_t = st.columns(2)
            with col_q:
                q_limit_opts = {"১০ টি": 10, "৩০ টি": 30, "৫০ টি": 50, "৭০ টি": 70, "১০০ টি": 100, "সবগুলো প্রশ্ন": 9999}
                q_limit = q_limit_opts[st.selectbox("কয়টি প্রশ্নের পরীক্ষা দেবেন?", list(q_limit_opts.keys()))]
            with col_t:
                time_opts = {"কোনো লিমিট নেই": 0, "৫ মিনিট": 5, "১০ মিনিট": 10, "২০ মিনিট": 20, "৩০ মিনিট": 30, "১ ঘণ্টা": 60}
                t_limit = time_opts[st.selectbox("সময় নির্ধারণ (টাইমার):", list(time_opts.keys()))]

            if st.button("🚀 পরীক্ষা শুরু করুন", type="primary", use_container_width=True):
                with st.spinner("প্রশ্ন প্রস্তুত করা হচ্ছে..."):
                    label = f"ক্যাটাগরি: {chosen_cat_name}" if chosen_cat_id else "সম্মিলিত পরীক্ষা (সব মিলিয়ে)"
                    if start_test(chosen_cat_id, q_limit, t_limit, label):
                        st.rerun()

    else:
        total_q = len(st.session_state.question_queue)
        progress_frac = (st.session_state.current_q_index / total_q) if total_q else 0.0

        st.markdown('<div class="app-hero"><h1>✍️ লাইভ সেলফ-অ্যাসেসমেন্ট</h1><p>' + st.session_state.test_type_label + '</p></div>', unsafe_allow_html=True)

        live_timer()

        st.progress(progress_frac, text=f"অগ্রগতি: {st.session_state.current_q_index}/{total_q}")

        col1, col2, col3 = st.columns(3)
        col1.metric("বর্তমান প্রশ্ন", f"{min(st.session_state.current_q_index + 1, total_q)}/{total_q}")
        col2.metric("সঠিক উত্তর", st.session_state.correct_count)
        current_percentage = (
            st.session_state.correct_count / st.session_state.total_attempted * 100
        ) if st.session_state.total_attempted > 0 else 0
        col3.metric("বর্তমান স্কোর", f"{current_percentage:.1f}%")

        st.markdown("---")

        with st.sidebar:
            st.markdown("### 🧭 নিয়ন্ত্রণ")
            if st.button("💾 পরীক্ষা শেষ ও সেভ করুন", type="primary", use_container_width=True):
                save_test_score(st.session_state.test_type_label, st.session_state.total_attempted, st.session_state.correct_count, current_percentage)
                reset_test_state()
                st.success("স্কোর সেভ হয়েছে! প্রগতি ট্যাবে চেক করুন।")
                st.rerun()

        test_finished = st.session_state.current_q_index >= total_q or st.session_state.current_q_id is None

        if st.session_state.time_is_up and not test_finished:
            st.error("⏰ সময় শেষ হয়ে গেছে! আপনার ফলাফল সেভ করুন।")
            if st.button("💾 ফলাফল সেভ করুন", type="primary", use_container_width=True):
                save_test_score(st.session_state.test_type_label, st.session_state.total_attempted, st.session_state.correct_count, current_percentage)
                reset_test_state()
                st.rerun()

        elif test_finished:
            st.success("🎉 অভিনন্দন! আপনি নির্বাচিত সবগুলো প্রশ্নের উত্তর দিয়েছেন।")
            st.balloons()
            if st.button("💾 ফলাফল সেভ করুন ও নতুন পরীক্ষা দিন", use_container_width=True):
                save_test_score(st.session_state.test_type_label, st.session_state.total_attempted, st.session_state.correct_count, current_percentage)
                reset_test_state()
                st.rerun()

        else:
            col_main, col_review = st.columns([1.5, 1], gap="large")

            with col_main:
                st.markdown(
                    f'<div class="question-card"><div class="big-question">'
                    f'প্রশ্ন {st.session_state.current_q_index + 1}: {st.session_state.current_q_text}'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )
                option_labels = [opt[0] for opt in st.session_state.current_options]
                option_indices = list(range(len(option_labels)))
                radio_key = f"radio_{st.session_state.current_q_id}_{st.session_state.current_q_index}"
                chosen_idx = st.radio(
                    "আপনার উত্তর বেছে নিন:", option_indices, index=None, key=radio_key,
                    label_visibility="collapsed",
                    format_func=lambda i: f"{chr(65 + i)})  {option_labels[i]}",
                )

                if chosen_idx is not None:
                    user_choice = option_labels[chosen_idx]
                    correct_answer = next(opt[0] for opt in st.session_state.current_options if opt[1] is True)
                    is_correct = user_choice == correct_answer

                    st.session_state.session_history.insert(0, {
                        "question": st.session_state.current_q_text,
                        "user_choice": user_choice,
                        "correct_answer": correct_answer,
                        "explanation": st.session_state.current_q_explain,
                        "source_url": st.session_state.current_q_source_url,
                        "is_correct": is_correct,
                    })

                    st.session_state.total_attempted += 1
                    if is_correct:
                        st.session_state.correct_count += 1
                        st.toast("✅ সঠিক উত্তর!", icon="✅")
                    else:
                        st.toast(f"❌ ভুল! সঠিক উত্তর: {correct_answer}", icon="❌")

                    st.session_state.current_q_index += 1
                    load_current_mcq()
                    st.rerun()

            with col_review:
                st.markdown("#### 🔍 সদ্য উত্তর দেয়া প্রশ্ন")
                if not st.session_state.session_history:
                    st.info("আপনি উত্তর দেয়া শুরু করলে এখানে এনালাইসিস দেখা যাবে।")
                else:
                    with st.container(height=500):
                        for idx, record in enumerate(st.session_state.session_history):
                            icon = "🟢" if record["is_correct"] else "🔴"
                            with st.expander(f"{icon} {record['question'][:60]}", expanded=(idx == 0)):
                                if record["is_correct"]:
                                    st.success(f"আপনার উত্তর: {record['user_choice']} ✓")
                                else:
                                    st.error(f"আপনার উত্তর: {record['user_choice']} ✗")
                                    st.info(f"সঠিক উত্তর: **{record['correct_answer']}**")
                                link_html = ""
                                if record.get("source_url"):
                                    link_html = f' <a href="{record["source_url"]}" target="_blank" class="source-link-icon" title="সূত্র দেখুন">🔗</a>'
                                st.markdown(f'<span>💡 {record["explanation"]}{link_html}</span>', unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# 10. MODE 3 — PROGRESS HISTORY
# -----------------------------------------------------------------------------
elif app_mode == "📈 প্রগতি ও হিস্ট্রি (Progress)":
    st.markdown('<div class="app-hero"><h1>📈 আপনার উন্নতির গ্রাফ</h1><p>নিজের অগ্রগতি ট্র্যাক করুন এবং দুর্বল জায়গাগুলো খুঁজে বের করুন।</p></div>', unsafe_allow_html=True)

    history_df = get_history()

    if history_df.empty:
        st.info("এখনো কোনো পরীক্ষার রেকর্ড নেই। লাইভ পরীক্ষা দিয়ে স্কোর সেভ করুন!")
    else:
        history_df["Date"] = pd.to_datetime(history_df["test_date"]).dt.strftime("%b %d, %Y %I:%M %p")
        streak = compute_streak(history_df)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("মোট পরীক্ষা", len(history_df))
        c2.metric("গড় স্কোর", f"{history_df['score_percentage'].mean():.1f}%")
        c3.metric("সর্বোচ্চ স্কোর", f"{history_df['score_percentage'].max():.1f}%")
        c4.metric("টানা দিন (Streak)", f"{streak} 🔥")

        st.markdown("#### স্কোর ট্রেন্ড (%)")
        st.line_chart(history_df.set_index("Date")["score_percentage"])

        st.markdown("#### গড় স্কোর — পরীক্ষার ধরন অনুযায়ী")
        by_type = history_df.groupby("test_type")["score_percentage"].mean().sort_values(ascending=False)
        st.bar_chart(by_type)

        st.markdown("#### পূর্ববর্তী পরীক্ষার বিস্তারিত")
        display_df = history_df[["Date", "test_type", "total_attempted", "correct_answers", "score_percentage"]].copy()
        display_df.columns = ["তারিখ", "ধরন", "মোট প্রশ্ন", "সঠিক উত্তর", "স্কোর (%)"]
        st.dataframe(display_df, use_container_width=True, hide_index=True)
