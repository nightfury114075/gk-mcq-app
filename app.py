import streamlit as st
import psycopg2
import pandas as pd

# -----------------------------------------------------------------------------
# 1. DATABASE CONNECTION SETUP (Production Version with Auto-Reconnect)
# -----------------------------------------------------------------------------
@st.cache_resource
def init_connection():
    """Connects to the cloud database using Streamlit Secrets."""
    return psycopg2.connect(
        host=st.secrets["DB_HOST"],
        database=st.secrets["DB_NAME"],
        user=st.secrets["DB_USER"],
        password=st.secrets["DB_PASSWORD"],
        port=st.secrets.get("DB_PORT", "5432")
    )

# --- THE PERMANENT FIX: Connection Ping & Auto-Reconnect ---
try:
    conn = init_connection()
    # 'Knock' on the database to see if the connection is still alive
    with conn.cursor() as cursor:
        cursor.execute("SELECT 1")
except (psycopg2.OperationalError, psycopg2.InterfaceError):
    # If Neon went to sleep and dropped the connection, clear cache and reconnect
    st.cache_resource.clear()
    conn = init_connection()
except Exception as e:
    st.error(f"Database Connection Error: {e}")
    st.stop()

# -----------------------------------------------------------------------------
# 2. HELPER UTILITIES & DATA FETCHING
# -----------------------------------------------------------------------------
def get_categories():
    with conn.cursor() as cursor:
        cursor.execute("SELECT category_id, category_name FROM categories ORDER BY category_name;")
        return cursor.fetchall()

def get_all_questions_by_category(category_id):
    query = """
        SELECT q.question_text, q.explanation, 
               string_agg(o.option_text || (CASE WHEN o.is_correct THEN ' (সঠিক)' ELSE '' END), ' | ') as all_options
        FROM questions q
        JOIN options o ON q.question_id = o.question_id
        WHERE q.category_id = %s
        GROUP BY q.question_id, q.question_text, q.explanation;
    """
    return pd.read_sql_query(query, conn, params=(category_id,))

def get_random_mcq(category_id=None):
    with conn.cursor() as cursor:
        if category_id:
            cursor.execute("SELECT question_id, question_text, explanation FROM questions WHERE category_id = %s ORDER BY RANDOM() LIMIT 1;", (category_id,))
        else:
            cursor.execute("SELECT question_id, question_text, explanation FROM questions ORDER BY RANDOM() LIMIT 1;")
        
        question = cursor.fetchone()
        if not question:
            return None, None, None, None
            
        q_id, q_text, q_explain = question
        cursor.execute("SELECT option_text, is_correct FROM options WHERE question_id = %s ORDER BY RANDOM();", (q_id,))
        options = cursor.fetchall()
        return q_id, q_text, q_explain, options

def save_test_score(test_type, attempted, correct, percentage):
    with conn.cursor() as cursor:
        cursor.execute(
            """INSERT INTO exam_history (test_type, total_attempted, correct_answers, score_percentage) 
               VALUES (%s, %s, %s, %s)""",
            (test_type, attempted, correct, percentage)
        )
        conn.commit()

def get_history():
    query = "SELECT test_date, test_type, total_attempted, correct_answers, score_percentage FROM exam_history ORDER BY test_date ASC;"
    return pd.read_sql_query(query, conn)

# -----------------------------------------------------------------------------
# 3. APPLICATION STATE MANAGEMENT
# -----------------------------------------------------------------------------
if "current_q_id" not in st.session_state:
    st.session_state.current_q_id = None
    st.session_state.current_q_text = None
    st.session_state.current_q_explain = None
    st.session_state.current_options = None
    st.session_state.total_attempted = 0
    st.session_state.correct_count = 0
    st.session_state.session_history = [] 

def load_new_mcq(cat_id):
    q_id, q_text, q_explain, options = get_random_mcq(cat_id)
    st.session_state.current_q_id = q_id
    st.session_state.current_q_text = q_text
    st.session_state.current_q_explain = q_explain
    st.session_state.current_options = options

def reset_score():
    st.session_state.total_attempted = 0
    st.session_state.correct_count = 0
    st.session_state.session_history = []

# -----------------------------------------------------------------------------
# 4. USER INTERFACE RENDERING
# -----------------------------------------------------------------------------
st.set_page_config(page_title="GK Exam Engine", page_icon="📚", layout="wide")
st.title("📚 সাধারণ জ্ঞান (GK) লার্নিং পোর্টাল")
st.markdown("---")

app_mode = st.sidebar.radio("একটি মোড নির্বাচন করুন:", ["পড়াশোনা (Study Mode)", "লাইভ পরীক্ষা (Live MCQ)", "প্রগতি ও হিস্ট্রি (Progress)"])

categories = get_categories()
category_options = {name: cid for cid, name in categories}

# --- MODE 1: STUDY MODE ---
if app_mode == "পড়াশোনা (Study Mode)":
    st.header("📖 তথ্য ভাণ্ডার ও রিভিশন")
    selected_cat_name = st.selectbox("ক্যাটাগরি বেছে নিন:", list(category_options.keys()))
    df = get_all_questions_by_category(category_options[selected_cat_name])
    
    if not df.empty:
        for idx, row in df.iterrows():
            st.markdown(f"**{idx + 1}. {row['question_text']}**")
            opts = row['all_options'].split(' | ')
            cols = st.columns(4)
            for i, opt in enumerate(opts):
                if "(সঠিক)" in opt: cols[i%4].markdown(f"✅ **{opt.replace(' (সঠিক)', '')}**")
                else: cols[i%4].markdown(f"⚪ {opt}")
            st.caption(f"💡 {row['explanation']}")
            st.markdown("---")

# --- MODE 2: LIVE MCQ TEST ---
elif app_mode == "লাইভ পরীক্ষা (Live MCQ)":
    st.header("✍️ লাইভ সেলফ-অ্যাসেসমেন্ট")
    
    col1, col2, col3 = st.columns(3)
    col1.metric("মোট উত্তর দিয়েছেন", st.session_state.total_attempted)
    col2.metric("সঠিক উত্তর", st.session_state.correct_count)
    
    current_percentage = 0
    if st.session_state.total_attempted > 0:
        current_percentage = (st.session_state.correct_count / st.session_state.total_attempted) * 100
    col3.metric("বর্তমান স্কোর (%)", f"{current_percentage:.1f}%")
    st.markdown("---")

    test_type = st.radio("পরীক্ষার ধরন:", ["নির্দিষ্ট ক্যাটাগরি", "সম্মিলিত পরীক্ষা"], horizontal=True)
    chosen_cat_id = category_options[st.selectbox("ক্যাটাগরি:", list(category_options.keys()))] if test_type == "নির্দিষ্ট ক্যাটাগরি" else None

    if st.session_state.total_attempted > 0:
        if st.sidebar.button("💾 পরীক্ষা শেষ করুন ও সেভ করুন", type="primary"):
            save_test_score(test_type, st.session_state.total_attempted, st.session_state.correct_count, current_percentage)
            reset_score()
            st.sidebar.success("স্কোর সেভ হয়েছে! প্রগতি ট্যাবে গিয়ে চেক করুন।")
            load_new_mcq(chosen_cat_id)
            st.rerun()

    if st.session_state.current_q_id is None:
        load_new_mcq(chosen_cat_id)
        st.rerun()

    col_main, col_review = st.columns([1.5, 1], gap="large")

    with col_main:
        st.subheader("বর্তমান প্রশ্ন")
        if st.session_state.current_q_text:
            with st.container(border=True):
                st.markdown(f"### {st.session_state.current_q_text}")
                option_labels = [opt[0] for opt in st.session_state.current_options]
                
                radio_key = f"radio_{st.session_state.current_q_id}"
                user_choice = st.radio("আপনার উত্তর বেছে নিন:", option_labels, index=None, key=radio_key)
                
                if user_choice:
                    correct_answer = next(opt[0] for opt in st.session_state.current_options if opt[1] is True)
                    is_correct = (user_choice == correct_answer)
                    
                    st.session_state.session_history.insert(0, {
                        "question": st.session_state.current_q_text,
                        "user_choice": user_choice,
                        "correct_answer": correct_answer,
                        "explanation": st.session_state.current_q_explain,
                        "is_correct": is_correct
                    })
                    
                    st.session_state.total_attempted += 1
                    if is_correct:
                        st.session_state.correct_count += 1
                        
                    load_new_mcq(chosen_cat_id)
                    st.rerun()

    with col_review:
        st.subheader("সদ্য উত্তর দেয়া প্রশ্ন (Analysis)")
        if not st.session_state.session_history:
            st.info("আপনি উত্তর দেয়া শুরু করলে এখানে এনালাইসিস দেখা যাবে।")
        else:
            with st.container(height=500):
                for idx, record in enumerate(st.session_state.session_history):
                    border_color = "🟢" if record['is_correct'] else "🔴"
                    with st.expander(f"{border_color} {record['question']}", expanded=(idx == 0)):
                        if record['is_correct']:
                            st.success(f"আপনার উত্তর: {record['user_choice']} (সঠিক)")
                        else:
                            st.error(f"আপনার উত্তর: {record['user_choice']} (ভুল)")
                            st.info(f"সঠিক উত্তর: **{record['correct_answer']}**")
                        st.caption(f"💡 **ব্যাখ্যা:** {record['explanation']}")

# --- MODE 3: PROGRESS HISTORY ---
elif app_mode == "প্রগতি ও হিস্ট্রি (Progress)":
    st.header("📈 আপনার উন্নতির গ্রাফ (Day-by-Day Improvement)")
    
    history_df = get_history()
    
    if history_df.empty:
        st.info("এখনো কোনো পরীক্ষার রেকর্ড নেই। লাইভ পরীক্ষা দিয়ে স্কোর সেভ করুন!")
    else:
        history_df['Date'] = pd.to_datetime(history_df['test_date']).dt.strftime('%b %d, %Y %I:%M %p')
        st.subheader("স্কোর ট্রেন্ড (%)")
        st.line_chart(history_df.set_index('Date')['score_percentage'])
        
        st.subheader("পূর্ববর্তী পরীক্ষার বিস্তারিত")
        display_df = history_df[['Date', 'test_type', 'total_attempted', 'correct_answers', 'score_percentage']].copy()
        display_df.columns = ["তারিখ", "ধরন", "মোট প্রশ্ন", "সঠিক উত্তর", "স্কোর (%)"]
        st.dataframe(display_df, use_container_width=True)
