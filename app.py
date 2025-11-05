import os
import re
import streamlit as st
import google.generativeai as genai
import pandas as pd
import requests
from PIL import Image
from io import BytesIO
from datetime import datetime
from difflib import SequenceMatcher

# ======================
# 🌐 ตั้งค่า ngrok สำหรับแชร์ผ่านอินเทอร์เน็ต
# ======================
try:
    from pyngrok import ngrok, conf
    NGROK_AVAILABLE = True
except ImportError:
    NGROK_AVAILABLE = False
    print("⚠️ ไม่พบ pyngrok - ติดตั้งด้วย: pip install pyngrok")

def setup_ngrok(port=8501):
    """ตั้งค่า ngrok tunnel"""
    if not NGROK_AVAILABLE:
        return None
    
    try:
        # ปิด tunnel เก่า (ถ้ามี)
        tunnels = ngrok.get_tunnels()
        for tunnel in tunnels:
            ngrok.disconnect(tunnel.public_url)
        
        # สร้าง tunnel ใหม่
        public_url = ngrok.connect(port, bind_tls=True)
        return public_url
    except Exception as e:
        print(f"❌ ไม่สามารถสร้าง ngrok tunnel: {str(e)}")
        return None

# ======================
# 🛠️ การตั้งค่าเริ่มต้น
# ======================

# ตั้งค่า API Keys (ไม่บังคับใช้แล้ว)
try:
    GEMINI_API_KEY_INSURVERSE = st.secrets["GEMINI_API_KEY_INSURVERSE"]
    genai.configure(api_key=GEMINI_API_KEY_INSURVERSE)
    model = genai.GenerativeModel('gemini-1.5-flash')
    GEMINI_AVAILABLE = True
except:
    GEMINI_AVAILABLE = False

# System prompt
PROMPT_WORKAW = """คุณเป็นผู้ช่วยผู้เชี่ยวชาญด้าน Embedded System ชื่อ "EmbedBot"
หน้าที่:
1. ตอบคำถามเกี่ยวกับ Embedded System โดยอ้างอิงจากเอกสารวิชาการเท่านั้น
2. ถ้าไม่มีคำตอบใน dataset ให้บอกว่า "อยู่นอกเหนือขอบเขตวิชา Embedded System"
"""

# ======================
# 🔄 ฟังก์ชันการทำงาน
# ======================

def clear_current_chat():
    """ล้างการสนทนาปัจจุบัน"""
    st.session_state["current_messages"] = [
        {"role": "model", "content": "🤖 EmbedBot สวัสดีครับ พร้อมตอบคำถามเกี่ยวกับ Embedded System แล้วครับ 😊"}
    ]
    st.session_state["conversation_context"] = {}

def clear_all_history():
    """ล้างประวัติการสนทนาทั้งหมด"""
    st.session_state["conversation_sessions"] = []
    st.session_state["current_session_id"] = None
    st.session_state["current_messages"] = [
        {"role": "model", "content": "🤖 EmbedBot สวัสดีครับ พร้อมตอบคำถามเกี่ยวกับ Embedded System แล้วครับ 😊"}
    ]
    st.session_state["conversation_context"] = {}

def load_excel_data(file_path="dataset.xlsx"):
    """โหลดข้อมูลจากไฟล์ Excel แบบง่ายและตรงไปตรงมา"""
    try:
        # ลองหาไฟล์จากหลายตำแหน่ง
        possible_paths = [
            file_path,
            f"./{file_path}",
            f"data/{file_path}",
            os.path.join(os.getcwd(), file_path)
        ]
        
        excel_file = None
        for path in possible_paths:
            if os.path.exists(path):
                excel_file = path
                st.success(f"✅ พบไฟล์ที่: {path}")
                break
        
        if not excel_file:
            st.error(f"❌ ไม่พบไฟล์ {file_path}")
            return pd.DataFrame()
        
        # อ่านไฟล์ Excel
        df = pd.read_excel(excel_file)
        
        # ตรวจสอบคอลัมน์ที่จำเป็น
        required_columns = ['หมวดหมู่', 'หัวข้อย่อย', 'คำถาม', 'คำตอบ', 'รูปภาพ']
        missing_cols = [col for col in required_columns if col not in df.columns]
        
        if missing_cols:
            st.error(f"❌ ขาดคอลัมน์: {missing_cols}")
            st.info(f"คอลัมน์ที่มี: {list(df.columns)}")
            return pd.DataFrame()
        
        # ทำความสะอาดข้อมูล
        df = df[required_columns].copy()
        df = df.fillna('')
        df = df[df['คำถาม'].str.strip() != '']
        df = df[df['คำตอบ'].str.strip() != '']
        
        # แปลงเป็น string ทั้งหมด
        for col in df.columns:
            df[col] = df[col].astype(str).str.strip()
        
        st.success(f"✅ โหลดข้อมูลสำเร็จ: {len(df)} คำถาม")
        return df
        
    except Exception as e:
        st.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
        return pd.DataFrame()

def similarity_score(str1, str2):
    """คำนวณความคล้ายคลึงระหว่างสองสตริง"""
    return SequenceMatcher(None, str1.lower(), str2.lower()).ratio()

def find_best_match(user_input, df, context, threshold=0.3):
    """
    ค้นหาคำถามที่ตรงที่สุดจาก dataset
    
    วิธีการค้นหา:
    1. ตรวจสอบคำถามที่ตรงทุกตัวอักษร (exact match)
    2. ตรวจสอบคำที่มีอยู่บางส่วน (partial match) - เหมาะกับคำสั้น
    3. ตรวจสอบคำถามที่มีคำสำคัญตรงกัน (keyword match)
    4. ตรวจสอบความคล้ายคลึง (similarity)
    5. พิจารณาบริบท (หมวดหมู่และหัวข้อย่อยเดิม)
    """
    if df.empty:
        return None
    
    user_lower = user_input.lower().strip()
    user_words = user_lower.split()
    
    # ปรับ threshold สำหรับคำถามสั้น
    if len(user_words) <= 3:
        threshold = 0.2
    
    best_match_idx = None
    best_score = 0
    
    for idx, row in df.iterrows():
        question = str(row['คำถาม']).lower().strip()
        question_words = question.split()
        
        # 1. Exact match (คะแนนเต็ม)
        if user_lower == question:
            return idx
        
        # 2. Partial match - ตรวจสอบว่าคำในคำถามผู้ใช้มีอยู่ในคำถาม dataset
        partial_match_score = 0
        for user_word in user_words:
            # ข้ามคำทั่วไป
            if user_word in ['คือ', 'อะไร', 'คือ?', 'อะไร?', 'ใช่', 'ไหม', 'หรือไม่']:
                continue
            
            # ตรวจสอบว่าคำนี้มีในคำถาม dataset หรือไม่
            for q_word in question_words:
                # Exact word match
                if user_word == q_word:
                    partial_match_score += 1.0
                # Partial word match (เช่น "embed" ใน "embedded")
                elif user_word in q_word or q_word in user_word:
                    if len(user_word) >= 3:  # คำต้องยาวพอสมควร
                        partial_match_score += 0.8
        
        # ปรับคะแนนตามจำนวนคำ
        if len(user_words) > 0:
            partial_match_score = partial_match_score / len(user_words)
        
        # 3. Keyword matching
        user_word_set = set(user_words)
        question_word_set = set(question_words)
        common_words = user_word_set.intersection(question_word_set)
        keyword_score = len(common_words) / max(len(user_word_set), len(question_word_set)) if len(user_word_set) > 0 else 0
        
        # 4. Similarity score
        sim_score = similarity_score(user_lower, question)
        
        # 5. Context bonus
        context_bonus = 0
        if context:
            if context.get('last_category') == row['หมวดหมู่']:
                context_bonus += 0.1
            if context.get('last_subcategory') == row['หัวข้อย่อย']:
                context_bonus += 0.1
        
        # คำนวณคะแนนรวม
        # ให้น้ำหนัก partial match มากสำหรับคำสั้น
        if len(user_words) <= 3:
            total_score = (partial_match_score * 0.5) + (keyword_score * 0.2) + (sim_score * 0.2) + context_bonus
        else:
            total_score = (partial_match_score * 0.3) + (keyword_score * 0.3) + (sim_score * 0.3) + context_bonus
        
        # โบนัสพิเศษสำหรับคำถามสั้นที่มี partial match สูง
        if len(user_words) <= 3 and partial_match_score > 0.6:
            total_score += 0.3
        
        if total_score > best_score:
            best_score = total_score
            best_match_idx = idx
    
    # คืนค่าถ้าคะแนนเกิน threshold
    if best_score >= threshold:
        return best_match_idx
    
    return None

def display_image_from_url(url, caption="รูปภาพประกอบ"):
    """แสดงรูปภาพจาก URL"""
    if not url or url == '' or url == 'nan':
        return False
    
    try:
        # ตรวจสอบว่าเป็น URL ที่ถูกต้อง
        if not url.startswith('http'):
            st.warning(f"⚠️ URL ไม่ถูกต้อง: {url}")
            return False
        
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        image = Image.open(BytesIO(response.content))
        st.image(image, caption=caption, use_container_width=True)
        return True
    except Exception as e:
        st.error(f"❌ ไม่สามารถโหลดรูปภาพ: {str(e)}\nURL: {url}")
        return False

def create_new_session():
    """สร้าง session การสนทนาใหม่"""
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_data = {
        "id": session_id,
        "title": f"การสนทนา {len(st.session_state.conversation_sessions) + 1}",
        "timestamp": datetime.now(),
        "messages": [
            {"role": "model", "content": "🤖 EmbedBot สวัสดีครับ พร้อมตอบคำถามเกี่ยวกับ Embedded System แล้วครับ 😊"}
        ],
        "preview": "การสนทนาใหม่",
        "context": {}
    }
    
    if "conversation_sessions" not in st.session_state:
        st.session_state.conversation_sessions = []
    
    st.session_state.conversation_sessions.append(session_data)
    st.session_state.current_session_id = session_id
    st.session_state.current_messages = session_data["messages"].copy()
    st.session_state.conversation_context = {}
    
    return session_id

def switch_session(session_id):
    """เปลี่ยนไปยัง session ที่เลือก"""
    for session in st.session_state.conversation_sessions:
        if session["id"] == session_id:
            st.session_state.current_session_id = session_id
            st.session_state.current_messages = session["messages"].copy()
            st.session_state.conversation_context = session.get("context", {})
            break

def update_session_preview(session_id, user_input):
    """อัพเดต preview ของ session"""
    for session in st.session_state.conversation_sessions:
        if session["id"] == session_id:
            if session["preview"] == "การสนทนาใหม่":
                preview = user_input[:50] + "..." if len(user_input) > 50 else user_input
                session["preview"] = preview
                session["title"] = preview
            break

def generate_response(user_input, df):
    """สร้างการตอบกลับจากข้อมูลใน dataset"""
    
    # คำสั่งพิเศษ
    if user_input.lower() in ["clear", "ล้าง", "reset", "เริ่มใหม่"]:
        clear_current_chat()
        st.rerun()
        return

    # สร้าง session ใหม่ถ้ายังไม่มี
    if "current_session_id" not in st.session_state or not st.session_state.current_session_id:
        create_new_session()

    # เริ่มต้น context ถ้ายังไม่มี
    if "conversation_context" not in st.session_state:
        st.session_state.conversation_context = {}

    # ตรวจสอบว่ามีข้อมูลใน dataset หรือไม่
    if df.empty:
        response_text = "❌ ยังไม่มีข้อมูลในระบบ โปรดตรวจสอบไฟล์ dataset.xlsx"
        st.session_state.current_messages.append({"role": "user", "content": user_input})
        st.session_state.current_messages.append({"role": "model", "content": response_text})
        return

    # ค้นหาคำตอบที่ตรงที่สุด
    context = st.session_state.conversation_context
    match_idx = find_best_match(user_input, df, context)
    
    # เพิ่มคำถามของผู้ใช้
    st.session_state.current_messages.append({"role": "user", "content": user_input})
    
    if match_idx is not None:
        # พบคำตอบใน dataset
        row = df.iloc[match_idx]
        
        category = row['หมวดหมู่']
        subcategory = row['หัวข้อย่อย']
        question = row['คำถาม']
        answer = row['คำตอบ']
        image_url = row['รูปภาพ']
        
        # อัพเดทบริบท
        st.session_state.conversation_context = {
            "last_category": category,
            "last_subcategory": subcategory,
            "last_question": question
        }
        
        # สร้างคำตอบ
        response_text = f"📖 **หมวดหมู่:** {category}\n"
        response_text += f"📂 **หัวข้อย่อย:** {subcategory}\n\n"
        response_text += f"❓ **คำถาม:** {question}\n\n"
        response_text += f"💡 **คำตอบ:**\n{answer}\n\n"
        
        # เพิ่มรูปภาพถ้ามี
        has_image = False
        if image_url and image_url != '' and image_url != 'nan':
            response_text += f"🖼️ **มีรูปภาพประกอบ** (แสดงด้านล่าง)\n\n"
            has_image = True
        
        response_text += "💬 **มีคำถามเพิ่มเติมหรือไม่ครับ?**"
        
        # เพิ่มคำตอบ
        st.session_state.current_messages.append({
            "role": "model", 
            "content": response_text,
            "image_url": image_url if has_image else None
        })
        
    else:
        # ไม่พบคำตอบใน dataset
        response_text = "❌ **ขออภัยครับ**\n\n"
        response_text += "คำถามนี้อยู่นอกเหนือขอบเขตวิชา Embedded System ที่มีในระบบ\n\n"
        response_text += "📚 **คำถามที่ระบบสามารถตอบได้ครอบคลุม:**\n"
        
        # แสดงหมวดหมู่ที่มี
        categories = df['หมวดหมู่'].unique()
        for cat in categories[:5]:
            response_text += f"• {cat}\n"
        
        response_text += "\n💡 **ลองถามคำถามอื่นที่เกี่ยวข้องกับหัวข้อเหล่านี้ดูครับ**"
        
        st.session_state.current_messages.append({
            "role": "model", 
            "content": response_text,
            "image_url": None
        })
    
    # อัพเดต session
    update_session_preview(st.session_state.current_session_id, user_input)
    
    for session in st.session_state.conversation_sessions:
        if session["id"] == st.session_state.current_session_id:
            session["messages"] = st.session_state.current_messages.copy()
            session["context"] = st.session_state.conversation_context.copy()
            break

def setup_quick_questions(df):
    """สร้างปุ่มคำถามแนะนำจาก dataset"""
    if df.empty:
        return {}
    
    categories = {}
    
    for idx, row in df.iterrows():
        category = row['หมวดหมู่']
        subcategory = row['หัวข้อย่อย']
        question = row['คำถาม']
        
        if category not in categories:
            categories[category] = {}
        
        if subcategory not in categories[category]:
            categories[category][subcategory] = []
        
        if len(categories[category][subcategory]) < 4:
            categories[category][subcategory].append(question)
    
    return categories

def handle_quick_question(question):
    """จัดการเมื่อกดปุ่มคำถามแนะนำ"""
    df = st.session_state.get("qa_df", pd.DataFrame())
    generate_response(question, df)
    st.rerun()

# ======================
# 🖥️ ส่วนติดต่อผู้ใช้
# ======================

st.set_page_config(page_title="EmbedBot Dataset", page_icon="🤖", layout="wide")

# CSS
st.markdown("""
<style>
    .quick-questions-section {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 1rem;
        border-radius: 10px;
        margin-bottom: 1rem;
        color: white;
    }
    
    .category-section {
        background-color: #f8f9fa;
        padding: 1rem;
        border-radius: 10px;
        margin-bottom: 1rem;
        border-left: 4px solid #667eea;
    }
    
    .subcategory-section {
        background-color: #e9ecef;
        padding: 0.8rem;
        border-radius: 8px;
        margin-bottom: 0.8rem;
    }
    
    .status-info {
        background-color: #d1ecf1;
        border: 1px solid #bee5eb;
        color: #0c5460;
        padding: 10px;
        border-radius: 5px;
        margin-bottom: 10px;
    }
</style>
""", unsafe_allow_html=True)

# เริ่มต้น session state
if "conversation_sessions" not in st.session_state:
    st.session_state.conversation_sessions = []

if "current_session_id" not in st.session_state:
    st.session_state.current_session_id = None

if "current_messages" not in st.session_state:
    st.session_state.current_messages = [
        {"role": "model", "content": "🤖 EmbedBot สวัสดีครับ พร้อมตอบคำถามเกี่ยวกับ Embedded System แล้วครับ 😊"}
    ]

if "conversation_context" not in st.session_state:
    st.session_state.conversation_context = {}

if "qa_df" not in st.session_state:
    with st.spinner("🔄 กำลังโหลดข้อมูลจาก Excel..."):
        st.session_state.qa_df = load_excel_data("dataset.xlsx")

# โหลดข้อมูล
df = st.session_state.get("qa_df", pd.DataFrame())

# ======================
# 🎯 ส่วนปุ่มคำถามแนะนำ
# ======================

quick_categories = setup_quick_questions(df)

if quick_categories and not df.empty:
    st.markdown('<div class="quick-questions-section">', unsafe_allow_html=True)
    st.markdown('<h3 style="text-align: center; color: white;">🚀 คำถามแนะนำจาก Dataset</h3>', unsafe_allow_html=True)
    
    for category, subcategories in list(quick_categories.items())[:2]:
        st.markdown(f'<div class="category-section">', unsafe_allow_html=True)
        st.subheader(f"📁 {category}")
        
        for subcategory, questions in list(subcategories.items())[:2]:
            st.markdown(f'<div class="subcategory-section">', unsafe_allow_html=True)
            st.write(f"**📂 {subcategory}**")
            
            cols = st.columns(2)
            for i, question in enumerate(questions[:4]):
                col_idx = i % 2
                with cols[col_idx]:
                    if st.button(
                        question[:60] + "..." if len(question) > 60 else question,
                        key=f"quick_{category}_{subcategory}_{i}",
                        use_container_width=True,
                        type="secondary"
                    ):
                        handle_quick_question(question)
            
            st.markdown('</div>', unsafe_allow_html=True)
        
        st.markdown('</div>', unsafe_allow_html=True)
    
    st.markdown('</div>', unsafe_allow_html=True)

# แสดงสถานะ
if not df.empty:
    st.markdown(
        f'<div class="status-info">✅ โหลดข้อมูลสำเร็จ: {len(df)} คำถาม | '
        f'{len(df["หมวดหมู่"].unique())} หมวดหมู่ | '
        f'{len(df[df["รูปภาพ"] != ""]["รูปภาพ"])} รูปภาพ</div>',
        unsafe_allow_html=True
    )
else:
    st.error("❌ ไม่สามารถโหลดข้อมูลจาก dataset.xlsx ได้")

# Sidebar
with st.sidebar:
    st.header("💬 ประวัติการสนทนา")
    
    # ======================
    # 🌐 ส่วน ngrok - แชร์แอปผ่านอินเทอร์เน็ต
    # ======================
    if NGROK_AVAILABLE:
        st.divider()
        st.subheader("🌐 แชร์แอปผ่านอินเทอร์เน็ต")
        
        if "ngrok_url" not in st.session_state:
            st.session_state.ngrok_url = None
        
        col_ngrok1, col_ngrok2 = st.columns([3, 1])
        
        with col_ngrok1:
            if st.session_state.ngrok_url:
                st.success("✅ เชื่อมต่อแล้ว")
            else:
                st.info("📡 ยังไม่ได้เชื่อมต่อ")
        
        with col_ngrok2:
            if st.session_state.ngrok_url:
                if st.button("🔴", key="stop_ngrok", help="หยุด ngrok"):
                    try:
                        ngrok.disconnect(st.session_state.ngrok_url)
                        st.session_state.ngrok_url = None
                        st.rerun()
                    except:
                        st.session_state.ngrok_url = None
                        st.rerun()
            else:
                if st.button("🟢", key="start_ngrok", help="เริ่ม ngrok"):
                    with st.spinner("🔄 กำลังสร้าง tunnel..."):
                        tunnel = setup_ngrok()
                        if tunnel:
                            st.session_state.ngrok_url = tunnel.public_url
                            st.rerun()
        
        if st.session_state.ngrok_url:
            st.text_input(
                "🔗 Public URL (คัดลอกส่งให้เพื่อน)",
                value=st.session_state.ngrok_url,
                key="public_url_display",
                help="คัดลอก URL นี้ส่งให้เพื่อนเพื่อเข้าใช้งาน"
            )
            st.caption("⚠️ ใช้ได้จนกว่าจะปิดโปรแกรม")
        else:
            st.caption("💡 กด 🟢 เพื่อสร้าง Public URL")
    else:
        st.divider()
        st.warning("⚠️ ต้องการแชร์แอป?\nติดตั้ง: `pip install pyngrok`")
    
    st.divider()
    # ======================
    
    if st.button("🆕 สร้างการสนทนาใหม่", key="new_chat", use_container_width=True):
        create_new_session()
        st.rerun()
    
    if st.button("🗑️ ล้างประวัติทั้งหมด", key="clear_all", use_container_width=True):
        clear_all_history()
        st.rerun()
    
    st.divider()
    
    # แสดงบริบทปัจจุบัน
    if st.session_state.conversation_context:
        st.subheader("🧠 บริบทปัจจุบัน")
        context = st.session_state.conversation_context
        
        with st.expander("รายละเอียดบริบท", expanded=False):
            if context.get("last_category"):
                st.info(f"**หมวดหมู่:** {context['last_category']}")
            if context.get("last_subcategory"):
                st.info(f"**หัวข้อย่อย:** {context['last_subcategory']}")
            if context.get("last_question"):
                st.info(f"**คำถามล่าสุด:** {context['last_question'][:50]}...")
    
    st.divider()
    
    # แสดงสถิติ
    if not df.empty:
        st.subheader("📊 สถิติ Dataset")
        st.metric("คำถามทั้งหมด", len(df))
        st.metric("หมวดหมู่", len(df['หมวดหมู่'].unique()))
        st.metric("รูปภาพ", len(df[df['รูปภาพ'] != '']))
    
    st.divider()
    
    # แสดงรายการการสนทนา
    if st.session_state.conversation_sessions:
        st.subheader("📝 รายการการสนทนา")
        
        for session in st.session_state.conversation_sessions:
            is_active = session["id"] == st.session_state.current_session_id
            
            col1, col2 = st.columns([4, 1])
            
            with col1:
                button_label = f"{'📍' if is_active else '📝'} {session['title'][:30]}..."
                
                if st.button(
                    button_label,
                    key=f"session_{session['id']}",
                    use_container_width=True,
                    type="primary" if is_active else "secondary"
                ):
                    switch_session(session["id"])
                    st.rerun()
            
            with col2:
                if st.button("🗑️", key=f"delete_{session['id']}", help="ลบ"):
                    st.session_state.conversation_sessions = [
                        s for s in st.session_state.conversation_sessions
                        if s["id"] != session["id"]
                    ]
                    if session["id"] == st.session_state.current_session_id:
                        if st.session_state.conversation_sessions:
                            switch_session(st.session_state.conversation_sessions[0]["id"])
                        else:
                            create_new_session()
                    st.rerun()
            
            time_str = session["timestamp"].strftime("%d/%m %H:%M")
            message_count = len([m for m in session["messages"] if m["role"] == "user"])
            st.caption(f"⏰ {time_str} | 💬 {message_count} คำถาม")
            st.divider()

# Layout หลัก
st.title("🤖 EmbedBot Dataset: ระบบตอบคำถามจาก Dataset")
st.caption("ตอบคำถามจากข้อมูลในไฟล์ Excel พร้อมแสดงรูปภาพ")

# แสดงข้อมูล ngrok (ถ้ามี)
if NGROK_AVAILABLE and st.session_state.get("ngrok_url"):
    st.success(f"🌐 **แอปพร้อมแชร์แล้ว!** ส่ง URL นี้ให้เพื่อน: `{st.session_state.ngrok_url}`")

st.divider()

# แสดงการสนทนา
chat_container = st.container()

with chat_container:
    st.subheader("💬 การสนทนาปัจจุบัน")
    
    for msg in st.session_state.current_messages:
        avatar = "🤖" if msg["role"] == "model" else "👤"
        
        with st.chat_message(msg["role"], avatar=avatar):
            st.write(msg["content"])
            
            # แสดงรูปภาพถ้ามี
            if msg["role"] == "model" and msg.get("image_url"):
                st.write("---")
                st.write("🖼️ **รูปภาพประกอบ:**")
                display_image_from_url(msg["image_url"])

# Input
st.divider()

st.info(
    "💡 **วิธีใช้งาน:**\n"
    "• พิมพ์คำถามที่ต้องการทราบ\n"
    "• ระบบจะค้นหาคำตอบจาก dataset อัตโนมัติ\n"
    "• ถ้าคำถามนอกเหนือจาก dataset ระบบจะแจ้งให้ทราบ\n"
    "• รูปภาพจะแสดงอัตโนมัติถ้ามีในคำตอบ"
)

user_input = st.chat_input("พิมพ์คำถามที่นี่...")

if user_input:
    with st.chat_message("user", avatar="👤"):
        st.write(user_input)
    
    with st.spinner("🔍 กำลังค้นหาข้อมูล..."):
        generate_response(user_input, df)
        st.rerun()

# Footer
st.divider()

# คำแนะนำ ngrok
if not NGROK_AVAILABLE:
    with st.expander("📡 ต้องการแชร์แอปให้เพื่อนใช้ผ่านอินเทอร์เน็ต?"):
        st.write("**ติดตั้ง pyngrok:**")
        st.code("pip install pyngrok", language="bash")
        st.write("**จากนั้นรีสตาร์ทแอป แล้วกดปุ่ม 🟢 ใน Sidebar**")
        st.info("💡 ngrok จะสร้าง Public URL ให้คุณแชร์ไปทั่วโลกได้ (ฟรี)")

st.caption("🤖 EmbedBot Dataset - ระบบตอบคำถามอัตโนมัติจาก Excel | ตอบตรงตามคอลัมน์ พร้อมแสดงรูปภาพ")