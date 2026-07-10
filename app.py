import streamlit as st
import psycopg2
import pandas as pd
import time

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
    with conn.cursor() as cursor:
        cursor.execute("SELECT 1")
except (psycopg2.OperationalError, psycopg2.InterfaceError):
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
    # Added: ORDER BY q.question_id ASC to keep input order
    query = """
        SELECT q.question_text, q.explanation, 
               string_agg(o.option_text || (CASE WHEN o.is_correct THEN ' (সঠিক)' ELSE '' END), ' | ') as all_options
        FROM questions q
        JOIN options o ON q.question_id = o.question_id
        WHERE q.category_id = %s
        GROUP BY q.question_id, q.question_text, q.explanation
        ORDER BY q.question_id ASC; 
    """
    return pd.read_sql_query(query, conn, params=(category_id,))

def get_randomized_question_ids(category_id=None):
    """Fetches all possible question IDs in random order."""
    with conn.cursor() as cursor:
        if category_id:
            cursor.execute("SELECT question_id FROM questions WHERE category_id = %s ORDER BY RANDOM();", (category_id,))
        else:
            cursor.execute("SELECT question_id FROM questions ORDER BY RANDOM();")
        return [row[0] for row in cursor.fetchall()]

def get_mcq_by_id(q_id):
    """Fetches a specific question and its options by ID."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT question_id, question_text, explanation FROM questions WHERE question_id = %s;", (q_id,))
        question = cursor.fetchone()
        if not question: return None, None, None, None
            
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
if "test_active" not in st.session_state:
    st.session_state.test_active = False
    st.session_state.question_queue = []     # Holds the randomized IDs for this session
    st.session_state.current_q_index = 0     # Tracks which question we are on
    st.session_state.end_timestamp = None    # For the countdown timer
    st.session_state.test_type_label = ""
    
    st.session_state.current_q_id = None
    st.session_state.current_q_text = None
    st.session_state.current_q_explain = None
    st.session_state.current_options = None
    st.session_state.total_attempted = 0
    st.session_state.correct_count = 0
    st.session_state.session_history = [] 

def load_current_mcq():
    """Loads the question from the queue based on current index."""
    if st.session_state.current_q_index < len(st.session_state.question_queue):
        q_id = st.session_state.question_queue[st.session_state.current_q_index]
        q_id, q_text, q_explain, options = get_mcq_by_id(q_id)
        st.session_state.current_q_id = q_id
        st.session_state.current_q_text = q_text
        st.session_state.current_q_explain = q_explain
        st.session_state.current_options = options
    else:
        st.session_state.current_q_id = None # End of test

def reset_test_state():
    st.session_state.test_active = False
    st.session_state.question_queue = []
    st.session_state.current_q_index = 0
    st.session_state.end_timestamp = None
    st.session_state.total_attempted = 0
    st.session_state.correct_count = 0
    st.session_state.session_history = []
    st.session_state.current_q_id = None

# --- JAVASCRIPT TIMER INJECTION ---
def render_timer():
    if st.session_state.end_timestamp:
        st.markdown(f"""
            <style>
            .timer-box {{
                font-size: 16px; font-weight: bold; color: #555;
                padding: 10px; border-radius: 8px; border: 1px solid #ddd;
                text-align: center; margin-bottom: 20px; background-color: #f9f9f9;
                transition: all 0.3s;
            }}
            </style>
            <div class="timer-box" id="timer_display">⏳ হিসাব করা হচ্ছে...</div>
            <script>
            var endTime = {st.session_state.end_timestamp * 1000};
            var timerInterval = setInterval(function() {{
                var now = new Date().getTime();
                var distance = endTime - now;
                if (distance < 0) {{
                    clearInterval(timerInterval);
                    document.getElementById("timer_display").innerHTML = "⏰ সময় শেষ! দয়া করে পরীক্ষা সেভ করুন।";
                    document.getElementById("timer_display").style.color = "white";
                    document.getElementById("timer_display").style.backgroundColor = "#D9534F";
                }} else {{
                    var minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
                    var seconds = Math.floor((distance % (1000 * 60)) / 1000);
                    document.getElementById("timer_display").innerHTML = "⏳ সময় বাকি: " + minutes + " মিনিট " + seconds + " সেকেন্ড";
                }}
            }}, 1000);
            </script>
        """, unsafe_allow_html=True)

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
    st.header("📖 তথ্য ভাণ্ডার ও রিভিশন (ক্রম অনুযায়ী)")
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
    else:
        st.info("এই ক্যাটাগরিতে এখনো কোনো প্রশ্ন যোগ করা হয়নি।")

# --- MODE 2: LIVE MCQ TEST ---
elif app_mode == "লাইভ পরীক্ষা (Live MCQ)":
    
    # SETUP PHASE: Before starting the exam
    if not st.session_state.test_active:
        st.header("⚙️ পরীক্ষার সেটিংস")
        with st.container(border=True):
            test_type = st.radio("পরীক্ষার ধরন নির্বাচন করুন:", ["নির্দিষ্ট ক্যাটাগরি", "সম্মিলিত পরীক্ষা (সব মিলিয়ে)"], horizontal=True)
            
            chosen_cat_id = None
            if test_type == "নির্দিষ্ট ক্যাটাগরি":
                chosen_cat_id = category_options[st.selectbox("ক্যাটাগরি বেছে নিন:", list(category_options.keys()))]
            
            col_q, col_t = st.columns(2)
            with col_q:
                q_limit_opts = {"১০ টি": 10, "৩০ টি": 30, "৫০ টি": 50, "৭০ টি": 70, "১০০ টি": 100, "সবগুলো প্রশ্ন": 9999}
                selected_q_limit_str = st.selectbox("কয়টি প্রশ্নের পরীক্ষা দেবেন?", list(q_limit_opts.keys()))
                q_limit = q_limit_opts[selected_q_limit_str]
                
            with col_t:
                time_opts = {"কোনো লিমিট নেই": 0, "৫ মিনিট": 5, "১০ মিনিট": 10, "২০ মিনিট": 20, "৩০ মিনিট": 30, "১ ঘণ্টা": 60}
                selected_time_str = st.selectbox("সময় নির্ধারণ (টাইমার):", list(time_opts.keys()))
                t_limit = time_opts[selected_time_str]

            if st.button("🚀 পরীক্ষা শুরু করুন", type="primary", use_container_width=True):
                all_ids = get_randomized_question_ids(chosen_cat_id)
                if not all_ids:
                    st.error("দুঃখিত, এই সেকশনে কোনো প্রশ্ন পাওয়া যায়নি।")
                else:
                    # Setup Session State
                    st.session_state.test_active = True
                    st.session_state.test_type_label = test_type
                    st.session_state.question_queue = all_ids[:q_limit] # Slice to user limit
                    st.session_state.current_q_index = 0
                    
                    if t_limit > 0:
                        st.session_state.end_timestamp = time.time() + (t_limit * 60)
                    else:
                        st.session_state.end_timestamp = None
                        
                    load_current_mcq()
                    st.rerun()

    # ACTIVE EXAM PHASE: Running the test
    else:
        st.header("✍️ লাইভ সেলফ-অ্যাসেসমেন্ট")
        total_q = len(st.session_state.question_queue)
        
        # Top Scoreboard & Controls
        col1, col2, col3 = st.columns(3)
        col1.metric(f"প্রশ্ন: {st.session_state.current_q_index} / {total_q}", "চলমান")
        col2.metric("সঠিক উত্তর", st.session_state.correct_count)
        current_percentage = (st.session_state.correct_count / st.session_state.total_attempted * 100) if st.session_state.total_attempted > 0 else 0
        col3.metric("বর্তমান স্কোর (%)", f"{current_percentage:.1f}%")
        
        # Render the Javascript Timer
        render_timer()
        st.markdown("---")

        # Sidebar Save & Exit Option
        if st.sidebar.button("💾 পরীক্ষা শেষ ও সেভ করুন", type="primary"):
            save_test_score(st.session_state.test_type_label, st.session_state.total_attempted, st.session_state.correct_count, current_percentage)
            reset_test_state()
            st.sidebar.success("স্কোর সেভ হয়েছে! প্রগতি ট্যাবে চেক করুন।")
            st.rerun()

        # Check if test is completed
        if st.session_state.current_q_index >= total_q or st.session_state.current_q_id is None:
            st.success("🎉 অভিনন্দন! আপনি নির্বাচিত সবগুলো প্রশ্নের উত্তর দিয়েছেন।")
            st.balloons()
            if st.button("💾 ফলাফল সেভ করুন ও নতুন পরীক্ষা দিন"):
                save_test_score(st.session_state.test_type_label, st.session_state.total_attempted, st.session_state.correct_count, current_percentage)
                reset_test_state()
                st.rerun()
                
        else:
            # Active Question UI
            col_main, col_review = st.columns([1.5, 1], gap="large")

            with col_main:
                st.subheader(f"প্রশ্ন নং {st.session_state.current_q_index + 1}")
                if st.session_state.current_q_text:
                    with st.container(border=True):
                        st.markdown(f"### {st.session_state.current_q_text}")
                        option_labels = [opt[0] for opt in st.session_state.current_options]
                        
                        radio_key = f"radio_{st.session_state.current_q_id}"
                        user_choice = st.radio("আপনার উত্তর বেছে নিন:", option_labels, index=None, key=radio_key)
                        
                        if user_choice:
                            correct_answer = next(opt[0] for opt in st.session_state.current_options if opt[1] is True)
                            is_correct = (user_choice == correct_answer)
                            
                            # Save to review history
                            st.session_state.session_history.insert(0, {
                                "question": st.session_state.current_q_text,
                                "user_choice": user_choice,
                                "correct_answer": correct_answer,
                                "explanation": st.session_state.current_q_explain,
                                "is_correct": is_correct
                            })
                            
                            # Update Scores & Index
                            st.session_state.total_attempted += 1
                            if is_correct:
                                st.session_state.correct_count += 1
                                
                            st.session_state.current_q_index += 1
                            load_current_mcq()
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
