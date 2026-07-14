import streamlit as st
import psycopg2
from psycopg2 import pool as pg_pool
import pandas as pd
import random
import time
from contextlib import contextmanager

# -----------------------------------------------------------------------------
# 0. PAGE CONFIG (must be first Streamlit call)
# -----------------------------------------------------------------------------
st.set_page_config(page_title="GK Exam Engine", page_icon="📚", layout="wide", initial_sidebar_state="expanded")

# -----------------------------------------------------------------------------
# 1. DATABASE LAYER (Connection Pool + Auto-Reconnect)
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

# -----------------------------------------------------------------------------
# 2. DATA ACCESS (Cached & decoupled from raw pandas DB connection)
# -----------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def get_categories():
    with get_cursor() as cur:
        cur.execute("SELECT category_id, category_name FROM categories ORDER BY category_name;")
        return cur.fetchall()

@st.cache_data(ttl=300, show_spinner=False)
def get_all_questions_by_category(category_id):
    query = """
        SELECT q.question_id, q.question_text, q.explanation, q.source_url,
               o.option_text, o.is_correct
        FROM questions q
        JOIN options o ON q.question_id = o.question_id
    """
    params = None
    if category_id is not None:
        query += " WHERE q.category_id = %s"
        params = (category_id,)
    query += " ORDER BY q.question_id ASC;"

    with get_cursor() as cur:
        if params:
            cur.execute(query, params)
        else:
            cur.execute(query)
        # Fixes SQLAlchemy UserWarning by converting directly from cursor results
        cols = [desc[0] for desc in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)

def group_questions(df):
    grouped = []
    for qid, g in df.groupby("question_id", sort=False):
        grouped.append({
            "question_id": qid,
            "question_text": g["question_text"].iloc[0],
            "explanation": g["explanation"].iloc[0],
            "source_url": g["source_url"].iloc[0] if "source_url" in g.columns else None,
            "options": list(zip(g["option_text"], g["is_correct"])),
        })
    return grouped

def get_randomized_question_ids(category_id=None, limit=None):
    with get_cursor() as cur:
        if category_id:
            cur.execute("SELECT question_id FROM questions WHERE category_id = %s ORDER BY RANDOM();", (category_id,))
        else:
            cur.execute("SELECT question_id FROM questions ORDER BY RANDOM();")
        ids = [r[0] for r in cur.fetchall()]
    return ids[:limit] if limit else ids

def fetch_question_bank(question_ids):
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
    with get_cursor() as cur:
        cur.execute(query)
        cols = [desc[0] for desc in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)

# -----------------------------------------------------------------------------
# 3. APPLICATION STATE
# -----------------------------------------------------------------------------
def init_state():
    defaults = {
        "test_active": False,
        "question_queue": [],
        "question_bank": {},
        "current_q_index": 0,
        "end_timestamp": None,
        "test_type_label": "",
        "current_q_id": None,
        "current_q_text": None,
        "current_q_explain": None,
        "current_q_source_url": None,
        "current_options": None,
        "total_attempted": 0,
        "correct_count": 0,
        "session_history": [],
        "study_visible": 50,
        "study_state_key": "",
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
    st.session_state.total_attempted = 0
    st.session_state.correct_count = 0
    st.session_state.session_history = []
    st.session_state.current_q_id = None

# -----------------------------------------------------------------------------
# 4. GLOBAL STYLING (Responsive, respects Dark/Light mode)
# -----------------------------------------------------------------------------
def inject_css():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Hind+Siliguri:wght@400;500;600;700&display=swap');

    html, body, [class*="css"] { font-family: 'Hind Siliguri', 'Segoe UI', sans-serif !important; }

    @keyframes fadeIn { from {opacity:0;} to {opacity:1;} }
    @keyframes slideIn { from {opacity:0; transform: translateY(14px);} to {opacity:1; transform: translateY(0);} }
    @keyframes popIn { from {opacity:0; transform: scale(.92);} to {opacity:1; transform: scale(1);} }

    .app-header {
        background: linear-gradient(135deg, var(--primary-color) 0%, #764ba2 100%);
        padding: 26px 32px; border-radius: 18px; color: #ffffff; margin-bottom: 22px;
        box-shadow: 0 10px 28px rgba(0,0,0,0.15); animation: fadeIn .5s ease;
    }
    .app-header h2 { margin: 0; color: #ffffff; }

    .question-card {
        background: var(--secondary-background-color); border-radius: 18px; padding: 26px 28px;
        box-shadow: 0 6px 22px rgba(0,0,0,0.08); border-left: 6px solid var(--primary-color);
        animation: slideIn .35s ease-out; margin-bottom: 18px;
    }
    .big-question { font-size: 24px; font-weight: 700; line-height: 1.55; color: var(--text-color); }
    .study-q { font-size: 19px; font-weight: 600; color: var(--text-color); margin-bottom: 10px; }

    .opt-correct {
        background: rgba(46, 204, 113, 0.1); color: #2ecc71; border: 1.5px solid #2ecc71; border-radius: 10px;
        padding: 8px 14px; margin-bottom: 8px; font-weight: 600; animation: popIn .3s ease;
    }
    .opt-normal {
        background: var(--background-color); color: var(--text-color); border: 1.5px solid var(--secondary-background-color); border-radius: 10px;
        padding: 8px 14px; margin-bottom: 8px;
    }

    div[role="radiogroup"] label {
        background: var(--background-color); border: 1.8px solid var(--secondary-background-color); border-radius: 12px;
        padding: 10px 16px; margin-bottom: 8px; transition: all 0.2s ease;
    }
    div[role="radiogroup"] label:hover { border-color: var(--primary-color); transform: translateX(3px); }

    .stButton>button { border-radius: 10px; font-weight: 600; transition: all .2s ease; border: none; }
    .stButton>button:hover { transform: translateY(-2px); box-shadow: 0 8px 18px rgba(0,0,0,0.15); }

    [data-testid="stMetric"] { background: var(--secondary-background-color); border-radius: 14px; padding: 12px 16px; border: 1px solid var(--background-color); }

    .source-link-icon { text-decoration: none !important; margin-left: 8px; font-size: 15px; }
    </style>
    """, unsafe_allow_html=True)

inject_css()

# -----------------------------------------------------------------------------
# 5. TIMER — Native Streamlit Fragment (No DOM leaks, server-side enforced)
# -----------------------------------------------------------------------------
@st.fragment(run_every=1)
def live_timer():
    if st.session_state.test_active and st.session_state.end_timestamp:
        time_left = st.session_state.end_timestamp - time.time()
        
        if time_left <= 0:
            st.error("⏰ সময় শেষ! পরীক্ষা স্বয়ংক্রিয়ভাবে বন্ধ হচ্ছে...")
            current_percentage = (
                st.session_state.correct_count / st.session_state.total_attempted * 100
            ) if st.session_state.total_attempted > 0 else 0
            
            save_test_score(
                st.session_state.test_type_label, 
                st.session_state.total_attempted, 
                st.session_state.correct_count, 
                current_percentage
            )
            reset_test_state()
            st.rerun()  # Forces parent script execution to close the test UI
        else:
            mins, secs = divmod(int(time_left), 60)
            if time_left <= 300: # 5 minutes warning
                st.warning(f"⏳ সময় বাকি: {mins:02d} : {secs:02d}")
            else:
                st.info(f"⏳ সময় বাকি: {mins:02d} : {secs:02d}")

# -----------------------------------------------------------------------------
# 6. SIDEBAR NAVIGATION
# -----------------------------------------------------------------------------
with st.sidebar:
    st.markdown("## 📚 GK Exam Engine")
    st.caption("BCS প্রস্তুতির জন্য স্মার্ট লার্নিং পোর্টাল")
    app_mode = st.radio(
        "মোড নির্বাচন করুন:",
        ["পড়াশোনা (Study Mode)", "লাইভ পরীক্ষা (Live MCQ)", "প্রগতি ও হিস্ট্রি (Progress)"],
        label_visibility="collapsed",
    )
    st.markdown("---")
    
    # Run the live timer only if test is currently active
    if st.session_state.test_active and st.session_state.end_timestamp:
        live_timer()
        st.markdown("---")

    if st.button("🔄 ডেটা রিফ্রেশ করুন", use_container_width=True):
        get_categories.clear()
        get_all_questions_by_category.clear()
        get_history.clear()
        st.rerun()

categories = get_categories()
category_options = {"সবগুলো ক্যাটাগরি (All)": None}
for cid, name in categories:
    category_options[name] = cid


# -----------------------------------------------------------------------------
# 7. MODE 1 — STUDY MODE 
# -----------------------------------------------------------------------------
if app_mode == "পড়াশোনা (Study Mode)":
    st.markdown('<div class="app-header"><h2>📖 তথ্য ভাণ্ডার ও রিভিশন</h2></div>', unsafe_allow_html=True)

    col_a, col_b = st.columns([2, 1])
    with col_a:
        selected_cat_name = st.selectbox("ক্যাটাগরি বেছে নিন:", list(category_options.keys()))
    with col_b:
        search_term = st.text_input("🔍 প্রশ্ন খুঁজুন", placeholder="কীওয়ার্ড লিখুন...")

    df = get_all_questions_by_category(category_options[selected_cat_name])

    state_key = f"{selected_cat_name}::{search_term}"
    if st.session_state.study_state_key != state_key:
        st.session_state.study_state_key = state_key
        st.session_state.study_visible = 50

    filtered = df[df["question_text"].str.contains(search_term, case=False, na=False)] if search_term else df
    grouped = group_questions(filtered)
    total_found = len(grouped)

    st.caption(f"📊 মোট {total_found} টি প্রশ্ন পাওয়া গেছে")

    if not grouped:
        st.info("এই ক্যাটাগরিতে এখনো কোনো প্রশ্ন যোগ করা হয়নি।")
    else:
        for i, item in enumerate(grouped[: st.session_state.study_visible]):
            with st.container(border=True):
                st.markdown(f'<div class="study-q">{i + 1}. {item["question_text"]}</div>', unsafe_allow_html=True)
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
                    st.markdown(f'{item["explanation"]}{link_html}', unsafe_allow_html=True)

        if st.session_state.study_visible < total_found:
            remaining = total_found - st.session_state.study_visible
            btn_col1, btn_col2 = st.columns(2)
            with btn_col1:
                if st.button(f"⬇️ আরও ১০০টি দেখুন ({remaining} টি বাকি)", use_container_width=True):
                    st.session_state.study_visible += 100
                    st.rerun()
            with btn_col2:
                if st.button(f"🔽 সবগুলো একসাথে দেখুন ({total_found} টি)", use_container_width=True):
                    st.session_state.study_visible = total_found
                    st.rerun()

# -----------------------------------------------------------------------------
# 8. MODE 2 — LIVE MCQ TEST
# -----------------------------------------------------------------------------
elif app_mode == "লাইভ পরীক্ষা (Live MCQ)":

    if not st.session_state.test_active:
        st.markdown('<div class="app-header"><h2>⚙️ পরীক্ষার সেটিংস</h2></div>', unsafe_allow_html=True)
        with st.container(border=True):
            test_type = st.radio(
                "পরীক্ষার ধরন নির্বাচন করুন:",
                ["নির্দিষ্ট ক্যাটাগরি", "সম্মিলিত পরীক্ষা (সব মিলিয়ে)"],
                horizontal=True,
            )

            chosen_cat_id = None
            if test_type == "নির্দিষ্ট ক্যাটাগরি":
                chosen_cat_id = category_options[st.selectbox("ক্যাটাগরি বেছে নিন:", list(category_options.keys()))]

            col_q, col_t = st.columns(2)
            with col_q:
                # Fixed: Magic number 9999 replaced with None for true "no limit"
                q_limit_opts = {"১০ টি": 10, "৩০ টি": 30, "৫০ টি": 50, "৭০ টি": 70, "১০০ টি": 100, "সবগুলো প্রশ্ন": None}
                q_limit = q_limit_opts[st.selectbox("কয়টি প্রশ্নের পরীক্ষা দেবেন?", list(q_limit_opts.keys()))]
            with col_t:
                time_opts = {"কোনো লিমিট নেই": 0, "৫ মিনিট": 5, "১০ মিনিট": 10, "২০ মিনিট": 20, "৩০ মিনিট": 30, "১ ঘণ্টা": 60}
                t_limit = time_opts[st.selectbox("সময় নির্ধারণ (টাইমার):", list(time_opts.keys()))]

            if st.button("🚀 পরীক্ষা শুরু করুন", type="primary", use_container_width=True):
                with st.spinner("প্রশ্ন প্রস্তুত করা হচ্ছে..."):
                    all_ids = get_randomized_question_ids(chosen_cat_id, limit=q_limit)
                    if not all_ids:
                        st.error("দুঃখিত, এই সেকশনে কোনো প্রশ্ন পাওয়া যায়নি।")
                    else:
                        st.session_state.question_bank = fetch_question_bank(all_ids)
                        st.session_state.test_active = True
                        st.session_state.test_type_label = test_type
                        st.session_state.question_queue = all_ids
                        st.session_state.current_q_index = 0
                        st.session_state.end_timestamp = time.time() + (t_limit * 60) if t_limit > 0 else None
                        load_current_mcq()
                        st.rerun()

    else:
        total_q = len(st.session_state.question_queue)
        progress_frac = (st.session_state.current_q_index / total_q) if total_q else 0.0

        st.markdown('<div class="app-header"><h2>✍️ লাইভ সেলফ-অ্যাসেসমেন্ট</h2></div>', unsafe_allow_html=True)
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

        if st.session_state.current_q_index >= total_q or st.session_state.current_q_id is None:
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
                radio_key = f"radio_{st.session_state.current_q_id}_{st.session_state.current_q_index}"
                user_choice = st.radio(
                    "আপনার উত্তর বেছে নিন:", option_labels, index=None, key=radio_key, label_visibility="collapsed"
                )

                if user_choice:
                    correct_answer = next(opt[0] for opt in st.session_state.current_options if opt[1] is True)
                    is_correct = user_choice == correct_answer

                    # Fixed: O(1) Append instead of O(n) Insert
                    st.session_state.session_history.append({
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
                        # Reverse iteration over list to show newest on top (O(1) list approach)
                        for idx, record in enumerate(reversed(st.session_state.session_history)):
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
# 9. MODE 3 — PROGRESS HISTORY
# -----------------------------------------------------------------------------
elif app_mode == "প্রগতি ও হিস্ট্রি (Progress)":
    st.markdown('<div class="app-header"><h2>📈 আপনার উন্নতির গ্রাফ</h2></div>', unsafe_allow_html=True)

    history_df = get_history()

    if history_df.empty:
        st.info("এখনো কোনো পরীক্ষার রেকর্ড নেই। লাইভ পরীক্ষা দিয়ে স্কোর সেভ করুন!")
    else:
        history_df["Date"] = pd.to_datetime(history_df["test_date"]).dt.strftime("%b %d, %Y %I:%M %p")

        c1, c2, c3 = st.columns(3)
        c1.metric("মোট পরীক্ষা", len(history_df))
        c2.metric("গড় স্কোর", f"{history_df['score_percentage'].mean():.1f}%")
        c3.metric("সর্বোচ্চ স্কোর", f"{history_df['score_percentage'].max():.1f}%")

        st.markdown("#### স্কোর ট্রেন্ড (%)")
        st.line_chart(history_df.set_index("Date")["score_percentage"])

        st.markdown("#### পূর্ববর্তী পরীক্ষার বিস্তারিত")
        display_df = history_df[["Date", "test_type", "total_attempted", "correct_answers", "score_percentage"]].copy()
        display_df.columns = ["তারিখ", "ধরন", "মোট প্রশ্ন", "সঠিক উত্তর", "স্কোর (%)"]
        st.dataframe(display_df, use_container_width=True, hide_index=True)
