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

# --- 1. СИСТЕМНІ НАЛАШТУВАННЯ ---
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Папка для моделей OCR
MODEL_DIR = os.path.join(os.getcwd(), "ocr_models")
if not os.path.exists(MODEL_DIR):
    os.makedirs(MODEL_DIR)

# --- 2. ПЕРЕВІРКА SECRETS ---
if "discord" not in st.secrets:
    st.error("Помилка: Налаштуйте секцію [discord] у Secrets на Streamlit Cloud!")
    st.stop()
config = st.secrets["discord"]

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- 3. БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 4. ЗАВАНТАЖЕННЯ OCR ---
@st.cache_resource(show_spinner=False)
def load_reader():
    with st.spinner("🏥 Завантаження модулів розпізнавання..."):
        return easyocr.Reader(['en', 'uk'], gpu=False, model_storage_directory=MODEL_DIR)

# --- 5. ЛОГІКА АВТОРИЗАЦІЇ (ОНОВЛЕНО) ---
def login_page():
    # Твоя актуальна адреса: https://ems-zvit.streamlit.app
    redirect_uri = config['DISCORD_REDIRECT_URI'].strip()
    scope = ['identify', 'guilds', 'guilds.members.read']
    
    discord = OAuth2Session(config['DISCORD_CLIENT_ID'], redirect_uri=redirect_uri, scope=scope)
    auth_url, _ = discord.authorization_url('https://discord.com/api/oauth2/authorize')

    st.title("🏥 MedBot ERP System")
    st.markdown("---")
    
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.info("👋 Вітаємо! Для початку роботи потрібно увійти.")
        
        # ВИКОРИСТОВУЄМО ПРЯМИЙ ТЕГ <a> ДЛЯ УНИКНЕННЯ ПОМИЛОК З'ЄДНАННЯ
        st.markdown(f'''
            <div style="text-align: center; margin-top: 20px;">
                <a href="{auth_url}" target="_self" style="
                    background-color: #5865F2; 
                    color: white; 
                    padding: 15px 40px; 
                    text-decoration: none; 
                    border-radius: 10px; 
                    font-weight: bold; 
                    font-size: 20px;
                    display: inline-block;
                    transition: 0.3s;
                    box-shadow: 0 4px 15px rgba(88,101,242,0.4);
                ">🔑 УВІЙТИ ЧЕРЕЗ DISCORD</a>
                <p style="margin-top: 15px; font-size: 14px; color: gray;">
                    Натисніть кнопку, щоб авторизуватися
                </p>
            </div>
        ''', unsafe_allow_html=True)

    # Обробка повернення (callback)
    params = st.query_params
    if "code" in params:
        try:
            token = discord.fetch_token('https://discord.com/api/oauth2/token',
                                        client_secret=config['DISCORD_CLIENT_SECRET'],
                                        code=params["code"])
            user_data = discord.get('https://discord.com/api/users/@me').json()
            u_id = user_data['id']

            # Перевірка ролі на сервері
            member_url = f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member"
            member_data = discord.get(member_url).json()
            roles = member_data.get('roles', [])
            
            is_admin = config['ADMIN_ROLE_ID'] in roles
            is_allowed = config['ALLOWED_ROLE_ID'] in roles or is_admin
            
            if not is_allowed:
                st.error("🚫 У вас немає доступу (відсутня роль на сервері).")
                return

            st.session_state.auth_user = {"id": u_id, "username": user_data['username'], "is_admin": is_admin}
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Помилка авторизації: {e}")

# Перевірка сесії
if 'auth_user' not in st.session_state:
    login_page()
    st.stop()

# --- 6. ОСНОВНИЙ ІНТЕРФЕЙС ---
reader = load_reader()
user = st.session_state.auth_user

# Функції бази даних
def get_coords(u_id):
    res = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    return json.loads(res[0]) if res else {"Surname": None, "Name": None, "ID": None}

def save_coords(u_id, data):
    cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (u_id, json.dumps(data)))
    conn.commit()

user_c = get_coords(user['id'])

st.sidebar.title(f"👤 {user['username']}")
menu = st.sidebar.radio("Меню", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.auth_user = None
    st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Калібрування")
    f = st.file_uploader("Завантажте зразок", type=['jpg', 'png', 'jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Поле", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='blue', return_type='box')
        if st.button("Зберегти зону"):
            user_c[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_coords(user['id'], user_c)
            st.success("Збережено!")

elif menu == "📄 Сканер":
    if not all(user_c.values()):
        st.warning("Налаштуйте зони в параметрах!")
    else:
        st.header("📸 Обробка документів")
        files = st.file_uploader("Фото паспортів", accept_multiple_files=True, type=['jpg', 'png', 'jpeg'])
        if files and st.button("🔍 Розпізнати"):
            results = []
            for f in files:
                img = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                data = {}
                for lbl, (x, y, w, h) in user_c.items():
                    crop = img[int(y):int(y+h), int(x):int(x+w)]
                    txt = " ".join([t[1] for t in reader.readtext(crop)])
                    data[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else txt.strip().capitalize()
                results.append(data)
            st.session_state.scan_res = results
            st.rerun()

        if 'scan_res' in st.session_state:
            st.subheader("📝 Коригування")
            final = []
            for i, item in enumerate(st.session_state.scan_res):
                c1, c2, c3 = st.columns(3)
                s = c1.text_input(f"Прізвище #{i+1}", item['Surname'], key=f"s{i}")
                n = c2.text_input(f"Ім'я #{i+1}", item['Name'], key=f"n{i}")
                u = c3.text_input(f"ID #{i+1}", item['ID'], key=f"u{i}")
                final.append({"Surname": s, "Name": n, "ID": u})
            
            if st.button("🚀 ВІДПРАВИТИ ЗВІТ"):
                msg = f"🏥 **Звіт** від <@{user['id']}>\n" + "\n".join([f"• {x['Surname']} {x['Name']} (ID: {x['ID']})" for x in final])
                requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": msg})
                cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final), datetime.now().strftime("%Y-%m-%d %H:%M")))
                conn.commit()
                st.success("Надіслано!")
                del st.session_state.scan_res
