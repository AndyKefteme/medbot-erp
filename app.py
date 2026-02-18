import streamlit as st
import cv2
import numpy as np
import easyocr
import requests
import json
import sqlite3
import re
import os
import io
import time
import sys
from PIL import Image
from streamlit_cropper import st_cropper
from datetime import datetime
from urllib.parse import quote

# --- 1. НАЛАШТУВАННЯ СИСТЕМИ ---
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
MODEL_DIR = os.path.join(os.getcwd(), "ocr_models")
if not os.path.exists(MODEL_DIR):
    os.makedirs(MODEL_DIR)

def log_it(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] [MEDBOT] {msg}")
    sys.stdout.flush()

# Перевірка Secrets
if "discord" not in st.secrets:
    st.error("Налаштуйте Secrets у Streamlit Cloud!")
    st.stop()
config = st.secrets["discord"]

st.set_page_config(layout="wide", page_title="MedBot ERP", page_icon="🏥")

# --- 2. БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 3. OCR ЗАВАНТАЖЕННЯ ---
@st.cache_resource(show_spinner=False)
def get_ocr_reader():
    log_it("Завантаження EasyOCR моделей...")
    return easyocr.Reader(['en', 'uk'], gpu=False, model_storage_directory=MODEL_DIR)

# --- 4. ФУНКЦІЇ АВТОРИЗАЦІЇ ---
def login_page():
    client_id = str(config['DISCORD_CLIENT_ID']).strip()
    redirect_uri = str(config['DISCORD_REDIRECT_URI']).strip()
    
    # Формуємо посилання вручну для надійності
    scope = quote("identify guilds guilds.members.read")
    encoded_redirect = quote(redirect_uri, safe='')
    auth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={encoded_redirect}"
        f"&response_type=code"
        f"&scope={scope}"
    )

    st.title("🏥 MedBot ERP System")
    st.divider()
    
    col1, col2 = st.columns([2, 1])
    with col1:
        st.subheader("Вхід у систему")
        st.info(f"Переконайтеся, що в Discord Developer Portal вказано: \n`{redirect_uri}`")
        st.markdown(f'''
            <a href="{auth_url}" target="_self" style="
                background-color: #5865F2; color: white; padding: 18px 40px; 
                text-decoration: none; border-radius: 10px; font-weight: bold; font-size: 20px;
                display: inline-block;
            ">🔑 УВІЙТИ ЧЕРЕЗ DISCORD</a>
        ''', unsafe_allow_html=True)

    with col2:
        st.subheader("Статус модулів")
        if 'reader' not in st.session_state:
            with st.spinner("Активація ШІ..."):
                st.session_state.reader = get_ocr_reader()
            st.success("✅ OCR Готовий")
            st.rerun()
        else:
            st.success("✅ OCR Активний")

    # Обробка Callback
    params = st.query_params
    if "code" in params:
        log_it("Обробка коду від Discord...")
        token_data = {
            'client_id': client_id,
            'client_secret': config['DISCORD_CLIENT_SECRET'],
            'grant_type': 'authorization_code',
            'code': params["code"],
            'redirect_uri': redirect_uri
        }
        r = requests.post('https://discord.com/api/oauth2/token', data=token_data)
        if r.status_code == 200:
            token = r.json()['access_token']
            headers = {"Authorization": f"Bearer {token}"}
            u_info = requests.get('https://discord.com/api/users/@me', headers=headers).json()
            
            # Перевірка ролі
            g_id = config['GUILD_ID']
            m_info = requests.get(f'https://discord.com/api/users/@me/guilds/{g_id}/member', headers=headers).json()
            
            roles = m_info.get('roles', [])
            is_admin = config['ADMIN_ROLE_ID'] in roles
            is_allowed = config['ALLOWED_ROLE_ID'] in roles or is_admin
            
            if is_allowed:
                st.session_state.auth_user = {"id": u_info['id'], "username": u_info['username'], "is_admin": is_admin}
                st.query_params.clear()
                st.rerun()
            else:
                st.error("🚫 У вас немає доступу до цієї системи.")
        else:
            st.error("Помилка авторизації. Перевірте Secrets.")

# --- ПЕРЕВІРКА СЕСІЇ ---
if 'auth_user' not in st.session_state:
    login_page()
    st.stop()

# --- 5. ГОЛОВНИЙ ФУНКЦІОНАЛ ---
user = st.session_state.auth_user
reader = st.session_state.reader

# Допоміжні функції
def get_user_coords(u_id):
    res = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    return json.loads(res[0]) if res else {"Surname": None, "Name": None, "ID": None}

def save_user_coords(u_id, data):
    cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (u_id, json.dumps(data)))
    conn.commit()

current_coords = get_user_coords(user['id'])

# Меню
st.sidebar.title(f"👤 {user['username']}")
page = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if page == "🚪 Вихід":
    st.session_state.clear()
    st.rerun()

elif page == "⚙️ Налаштування":
    st.header("📐 Калібрування зон")
    file = st.file_uploader("Завантажте зразок", type=['jpg', 'png', 'jpeg'])
    if file:
        img = Image.open(file).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Поле", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='red', return_type='box')
        if st.button("Зберегти зону"):
            current_coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_user_coords(user['id'], current_coords)
            st.success(f"Зону {target} збережено!")

elif page == "📄 Сканер":
    if not all(current_coords.values()):
        st.warning("Спочатку налаштуйте зони в 'Налаштуваннях'")
    else:
        st.header("📸 Сканування документів")
        up_files = st.file_uploader("Фото паспортів", accept_multiple_files=True, type=['jpg', 'png'])
        if up_files and st.button("🔍 Почати"):
            results = []
            for f in up_files:
                img_np = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                data = {}
                for lbl, (x, y, w, h) in current_coords.items():
                    crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                    txt_list = reader.readtext(crop)
                    text = " ".join([t[1] for t in txt_list])
                    data[lbl] = "".join(re.findall(r'\d+', text)) if lbl == "ID" else text.strip().capitalize()
                results.append(data)
            st.session_state.scan_results = results
            st.rerun()

        if 'scan_results' in st.session_state:
            st.subheader("Перевірка та відправка")
            final_list = []
            for i, res in enumerate(st.session_state.scan_results):
                c1, c2, c3 = st.columns(3)
                s = c1.text_input(f"Прізвище #{i}", res['Surname'], key=f"s{i}")
                n = c2.text_input(f"Ім'я #{i}", res['Name'], key=f"n{i}")
                u = c3.text_input(f"ID #{i}", res['ID'], key=f"u{i}")
                final_list.append({"Surname": s, "Name": n, "ID": u})
            
            if st.button("🚀 ВІДПРАВИТИ В DISCORD"):
                report = f"🏥 **Звіт від** <@{user['id']}>\n" + "\n".join([f"• {x['Surname']} {x['Name']} (ID: {x['ID']})" for x in final_list])
                requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": report})
                cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final_list), datetime.now().strftime("%Y-%m-%d %H:%M")))
                conn.commit()
                st.success("Надіслано!")
                del st.session_state.scan_results

elif page == "📊 Адмін-панель":
    if user['is_admin']:
        st.header("📊 Статистика")
        logs = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 50").fetchall()
        st.table([{"Юзер": r[1], "К-сть": r[2], "Час": r[3]} for r in logs])
    else:
        st.error("Доступ заборонено")
