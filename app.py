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
from PIL import Image
from streamlit_cropper import st_cropper
from datetime import datetime
from requests_oauthlib import OAuth2Session

# Дозволяємо OAuth працювати через проксі Streamlit
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Створюємо папку для моделей у корені, щоб Streamlit її бачив
MODEL_DIR = os.path.join(os.getcwd(), "ocr_models")
if not os.path.exists(MODEL_DIR):
    os.makedirs(MODEL_DIR)

# --- КОНФІГУРАЦІЯ SECRETS ---
if "discord" not in st.secrets:
    st.error("❌ Помилка: Secrets не налаштовані в Streamlit Cloud!")
    st.stop()
config = st.secrets["discord"]

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- ЗАВАНТАЖЕННЯ OCR (ОПТИМІЗОВАНО) ---
@st.cache_resource(show_spinner=False)
def get_reader():
    placeholder = st.empty()
    with placeholder.container():
        st.warning("⏳ Зачекайте 1-2 хвилини... Йде активація штучного інтелекту.")
        progress_bar = st.progress(0)
        # Форсуємо завантаження в конкретну папку
        reader = easyocr.Reader(['en', 'uk'], gpu=False, model_storage_directory=MODEL_DIR)
        progress_bar.progress(100)
        st.success("✅ Готово! Можна працювати.")
        time.sleep(1)
    placeholder.empty()
    return reader

# --- АВТОРИЗАЦІЯ ---
def handle_login():
    # ВАЖЛИВО: redirect_uri має бути ТОЧНО таким, як у Discord Developer Portal
    redirect_uri = config['DISCORD_REDIRECT_URI']
    scope = ['identify', 'guilds', 'guilds.members.read']
    
    discord = OAuth2Session(config['DISCORD_CLIENT_ID'], redirect_uri=redirect_uri, scope=scope)
    authorization_url, state = discord.authorization_url('https://discord.com/api/oauth2/authorize')

    st.title("🏥 MedBot ERP System")
    st.write("---")
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.info("Потрібна авторизація через ваш Discord аккаунт.")
        # Використовуємо звичайну кнопку-посилання
        st.markdown(f'''
            <div style="text-align: center;">
                <a href="{authorization_url}" target="_self" style="
                    background-color: #5865F2; color: white; padding: 15px 30px; 
                    text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 18px;
                    display: inline-block;
                ">🔑 УВІЙТИ ЧЕРЕЗ DISCORD</a>
            </div>
        ''', unsafe_allow_html=True)

    # Обробка повернення (callback)
    params = st.query_params
    if "code" in params:
        try:
            token = discord.fetch_token(
                'https://discord.com/api/oauth2/token',
                client_secret=config['DISCORD_CLIENT_SECRET'],
                code=params["code"]
            )
            user_data = discord.get('https://discord.com/api/users/@me').json()
            u_id = user_data['id']
            
            # Перевірка бану
            if cursor.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (u_id,)).fetchone():
                st.error("🚫 Ваш доступ заблоковано.")
                st.stop()

            # Перевірка ролі
            member_url = f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member"
            member_data = discord.get(member_url).json()
            roles = member_data.get('roles', [])
            
            is_admin = config['ADMIN_ROLE_ID'] in roles
            is_allowed = config['ALLOWED_ROLE_ID'] in roles or is_admin
            
            if not is_allowed:
                st.error("🚫 У вас немає доступу (потрібна роль у Discord).")
                st.stop()

            st.session_state.auth_user = {"id": u_id, "username": user_data['username'], "is_admin": is_admin}
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Помилка входу: {e}. Перевірте Redirect URI в налаштуваннях Discord!")

# Перевірка сесії
if 'auth_user' not in st.session_state:
    handle_login()
    st.stop()

# Якщо залогінені - завантажуємо OCR та інтерфейс
reader = get_reader()
user = st.session_state.auth_user

# --- ІНТЕРФЕЙС (МЕНЮ) ---
st.sidebar.success(f"Ви увійшли як: {user['username']}")
page = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вийти"])

if page == "🚪 Вийти":
    st.session_state.auth_user = None
    st.query_params.clear()
    st.rerun()

# Функції збереження/завантаження координат
def save_coords(u_id, c):
    cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (u_id, json.dumps(c)))
    conn.commit()

def load_coords(u_id):
    r = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    return json.loads(r[0]) if r else {"Surname": None, "Name": None, "ID": None}

current_c = load_coords(user['id'])

if page == "📊 Адмін-панель":
    if not user['is_admin']:
        st.error("Доступ лише для адмінів.")
    else:
        st.header("🛡 Управління системою")
        # Тут ваш код логів...
        logs = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 20").fetchall()
        st.table([{"Юзер": r[1], "Кількість": r[2], "Час": r[3]} for r in logs])

elif page == "⚙️ Налаштування":
    st.header("📐 Калібрування")
    img_file = st.file_uploader("Зразок документа", type=['jpg', 'png', 'jpeg'])
    if img_file:
        img = Image.open(img_file).convert("RGB").resize((1920, 1080))
        label = st.selectbox("Виберіть поле", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='red', return_type='box')
        if st.button("Зберегти зону"):
            current_c[label] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_coords(user['id'], current_c)
            st.success(f"Зону {label} оновлено!")

elif page == "📄 Сканер":
    if not all(current_c.values()):
        st.warning("Спочатку налаштуйте зони в 'Налаштуваннях'")
    else:
        st.header("📸 Сканування")
        files = st.file_uploader("Фото паспортів", accept_multiple_files=True, type=['jpg', 'png'])
        if files and st.button("Розпізнати"):
            results = []
            for f in files:
                img = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                data = {}
                for lbl, (x, y, w, h) in current_c.items():
                    crop = img[int(y):int(y+h), int(x):int(x+w)]
                    txt = " ".join([t[1] for t in reader.readtext(crop)])
                    data[lbl] = "".join(re.findall(r'\d+', txt)) if lbl=="ID" else txt.strip().capitalize()
                results.append(data)
            st.session_state.temp_results = results
            st.rerun()
            
        if 'temp_results' in st.session_state:
            for idx, res in enumerate(st.session_state.temp_results):
                c1, c2, c3 = st.columns(3)
                res['Surname'] = c1.text_input(f"Прізвище {idx}", res['Surname'])
                res['Name'] = c2.text_input(f"Ім'я {idx}", res['Name'])
                res['ID'] = c3.text_input(f"ID {idx}", res['ID'])
            
            if st.button("🚀 ВІДПРАВИТИ В DISCORD"):
                # Тут ваш код відправки через Webhook...
                requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": f"🏥 Звіт від {user['username']}"})
                cursor.execute("INSERT INTO logs VALUES (?,?,?,?)", (user['id'], user['username'], len(st.session_state.temp_results), datetime.now().strftime("%H:%M")))
                conn.commit()
                st.success("Надіслано!")
                del st.session_state.temp_results
