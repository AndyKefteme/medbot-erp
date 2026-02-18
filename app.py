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
import sys
from PIL import Image
from streamlit_cropper import st_cropper
from datetime import datetime
from urllib.parse import quote

# --- 1. ЗАВАНТАЖЕННЯ КОНФІГУРАЦІЇ ---
def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        st.error(f"❌ Не вдалося прочитати config.json! Переконайтеся, що файл завантажено на GitHub.")
        st.stop()

config = load_config()

# Шлях для моделей OCR
MODEL_DIR = os.path.join(os.getcwd(), "ocr_models")
if not os.path.exists(MODEL_DIR):
    os.makedirs(MODEL_DIR)

st.set_page_config(layout="wide", page_title="MedBot ERP", page_icon="🏥")

# --- 2. БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 3. OCR (КЕШУВАННЯ) ---
@st.cache_resource(show_spinner=False)
def get_ocr_reader():
    # Завантажуємо моделі один раз при старті
    return easyocr.Reader(['en', 'uk'], gpu=False, model_storage_directory=MODEL_DIR)

# --- 4. СТОРІНКА ЛОГІНУ ---
def show_login():
    client_id = str(config['DISCORD_CLIENT_ID']).strip()
    redirect_uri = str(config['DISCORD_REDIRECT_URI']).strip()
    
    # Створюємо чисте посилання для авторизації
    scope = quote("identify guilds guilds.members.read")
    encoded_redirect = quote(redirect_uri, safe='')
    auth_url = (f"https://discord.com/api/oauth2/authorize?client_id={client_id}"
                f"&redirect_uri={encoded_redirect}&response_type=code&scope={scope}")

    st.title("🏥 MedBot ERP System")
    st.divider()
    
    col1, col2 = st.columns([2, 1])
    with col1:
        st.subheader("Авторизація через Discord")
        st.warning(f"⚠️ Твій поточний Redirect URI: `{redirect_uri}`")
        st.info("Якщо після натискання кнопки Discord видає помилку — значить цей URI не збігається з тим, що вказано в Discord Developer Portal!")
        
        st.markdown(f'''
            <div style="margin: 20px 0;">
                <a href="{auth_url}" target="_self" style="
                    background-color: #5865F2; color: white; padding: 20px 50px; 
                    text-decoration: none; border-radius: 12px; font-weight: bold; font-size: 24px;
                    display: inline-block; box-shadow: 0 4px 15px rgba(0,0,0,0.2);
                ">🔑 УВІЙТИ ЧЕРЕЗ DISCORD</a>
            </div>
        ''', unsafe_allow_html=True)

    with col2:
        st.subheader("Статус системи")
        if 'reader' not in st.session_state:
            with st.spinner("Завантаження ШІ-моделей..."):
                st.session_state.reader = get_ocr_reader()
            st.success("✅ OCR Готовий")
            st.rerun()
        else:
            st.success("✅ OCR Працює")
            st.success("✅ База даних підключена")

    # Обробка коду від Discord
    params = st.query_params
    if "code" in params:
        code = params["code"]
        data = {
            'client_id': client_id,
            'client_secret': config['DISCORD_CLIENT_SECRET'],
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_uri
        }
        r = requests.post('https://discord.com/api/oauth2/token', data=data)
        if r.status_code == 200:
            token = r.json()['access_token']
            headers = {"Authorization": f"Bearer {token}"}
            u_info = requests.get('https://discord.com/api/users/@me', headers=headers).json()
            
            # Перевірка ролі на сервері
            g_id = config['GUILD_ID']
            m_resp = requests.get(f'https://discord.com/api/users/@me/guilds/{g_id}/member', headers=headers)
            
            if m_resp.status_code == 200:
                roles = m_resp.json().get('roles', [])
                is_admin = config['ADMIN_ROLE_ID'] in roles
                is_allowed = config['ALLOWED_ROLE_ID'] in roles or is_admin
                
                if is_allowed:
                    st.session_state.auth_user = {"id": u_info['id'], "username": u_info['username'], "is_admin": is_admin}
                    st.query_params.clear()
                    st.rerun()
                else:
                    st.error("🚫 У вас немає потрібної ролі в Discord.")
            else:
                st.error("❌ Ви не є учасником сервера.")
        else:
            st.error(f"Помилка Discord API: {r.json().get('error_description', r.text)}")

if 'auth_user' not in st.session_state:
    show_login()
    st.stop()

# --- 5. ОСНОВНЕ МЕНЮ (ПІСЛЯ ВХОДУ) ---
user = st.session_state.auth_user
reader = st.session_state.reader

def get_coords(u_id):
    res = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    return json.loads(res[0]) if res else {"Surname": None, "Name": None, "ID": None}

current_coords = get_coords(user['id'])

st.sidebar.title(f"👤 {user['username']}")
menu = st.sidebar.radio("Меню", ["📄 Сканер", "⚙️ Налаштування", "📊 Логи", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.clear()
    st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Налаштування зон")
    f = st.file_uploader("Завантажте зразок", type=['jpg', 'png'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Поле", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='blue', return_type='box')
        if st.button("Зберегти зону"):
            current_coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (user['id'], json.dumps(current_coords)))
            conn.commit()
            st.success(f"Зону {target} збережено!")

elif menu == "📄 Сканер":
    if not all(current_coords.values()):
        st.warning("Спочатку налаштуйте зони у вкладці 'Налаштування'!")
    else:
        st.header("📸 Сканування")
        files = st.file_uploader("Фото", accept_multiple_files=True)
        if files and st.button("🔍 Розпізнати"):
            res_list = []
            for f in files:
                img = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                d = {}
                for lbl, (x, y, w, h) in current_coords.items():
                    crop = img[int(y):int(y+h), int(x):int(x+w)]
                    txt = " ".join([t[1] for t in reader.readtext(crop)])
                    d[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else txt.strip().capitalize()
                res_list.append(d)
            st.session_state.scan_res = res_list
            st.rerun()

        if 'scan_res' in st.session_state:
            final = []
            for i, r in enumerate(st.session_state.scan_res):
                c1, c2, c3 = st.columns(3)
                s = c1.text_input(f"Прізвище {i}", r['Surname'], key=f"s{i}")
                n = c2.text_input(f"Ім'я {i}", r['Name'], key=f"n{i}")
                u = c3.text_input(f"ID {i}", r['ID'], key=f"u{i}")
                final.append({"Surname": s, "Name": n, "ID": u})
            
            if st.button("🚀 ВІДПРАВИТИ В DISCORD"):
                msg = f"🏥 **Звіт від** <@{user['id']}>\n" + "\n".join([f"• {x['Surname']} {x['Name']} ID:{x['ID']}" for x in final])
                requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": msg})
                cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final), datetime.now().strftime("%d.%m %H:%M")))
                conn.commit()
                st.success("✅ Надіслано!")
                del st.session_state.scan_res

elif menu == "📊 Логи":
    if user['is_admin']:
        logs = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC").fetchall()
        st.table([{"Юзер": r[1], "К-сть": r[2], "Час": r[3]} for r in logs])
