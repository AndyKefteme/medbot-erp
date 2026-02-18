import streamlit as st
import streamlit.components.v1 as components
import cv2
import numpy as np
import pytesseract
import requests
import json
import sqlite3
import re
import os
import io
from PIL import Image
from streamlit_cropper import st_cropper
from datetime import datetime
from requests_oauthlib import OAuth2Session

# Логування
def log(msg):
    print(f"[MEDBOT_LOG] {msg}")

# --- 0. НАЛАШТУВАННЯ LINUX ---
if os.path.exists('/usr/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# --- 1. КОНФІГУРАЦІЯ ---
try:
    config = {
        "DISCORD_CLIENT_ID": st.secrets["DISCORD_CLIENT_ID"],
        "DISCORD_CLIENT_SECRET": st.secrets["DISCORD_CLIENT_SECRET"],
        "DISCORD_REDIRECT_URI": st.secrets["DISCORD_REDIRECT_URI"],
        "GUILD_ID": st.secrets["GUILD_ID"],
        "ADMIN_ROLE_ID": st.secrets["ADMIN_ROLE_ID"],
        "ALLOWED_ROLE_ID": st.secrets["ALLOWED_ROLE_ID"],
        "DISCORD_WEBHOOK_URL": st.secrets["DISCORD_WEBHOOK_URL"]
    }
except Exception as e:
    st.error(f"Помилка конфігурації: {e}")
    st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro")

# --- 2. БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 3. АВТОРИЗАЦІЯ (ВИПРАВЛЕНИЙ ЦИКЛ) ---
if 'auth_user' not in st.session_state:
    st.session_state.auth_user = None

def handle_discord_login():
    # Отримуємо параметри URL
    params = st.query_params
    
    # ЯКЩО МИ ПОВЕРНУЛИСЯ ВІД DISCORD (є параметр code)
    if "code" in params:
        log("Виявлено код у параметрах, починаємо обмін на токен...")
        code = params["code"]
        try:
            discord = OAuth2Session(config['DISCORD_CLIENT_ID'], 
                                    redirect_uri=config['DISCORD_REDIRECT_URI'], 
                                    scope=["identify", "guilds", "guilds.members.read"])
            token = discord.fetch_token('https://discord.com/api/oauth2/token', 
                                        client_secret=config['DISCORD_CLIENT_SECRET'], 
                                        code=code)
            
            u_data = discord.get('https://discord.com/api/users/@me').json()
            m_data = discord.get(f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member").json()
            
            u_roles = m_data.get('roles', [])
            is_adm = config['ADMIN_ROLE_ID'] in u_roles
            if config['ALLOWED_ROLE_ID'] in u_roles or is_adm:
                st.session_state.auth_user = {"id": u_data['id'], "username": u_data['username'], "is_admin": is_adm}
                st.query_params.clear() # Очищуємо URL
                st.rerun()
            else:
                st.error("❌ Немає доступу (роль не знайдена).")
        except Exception as e:
            st.error(f"Помилка OAuth: {e}")
            if st.button("Спробувати ще раз"):
                st.query_params.clear()
                st.rerun()
        st.stop()

    # ЯКЩО МИ ЩЕ НЕ НАТИСНУЛИ КНОПКУ (початковий стан)
    st.title("🏥 MedBot ERP System")
    st.info("Будь ласка, авторизуйтесь.")
    
    auth_url = (f"https://discord.com/api/oauth2/authorize?client_id={config['DISCORD_CLIENT_ID']}&"
                f"redirect_uri={requests.utils.quote(config['DISCORD_REDIRECT_URI'])}&"
                f"response_type=code&scope=identify%20guilds%20guilds.members.read")

    # Створюємо кнопку через HTML, щоб уникнути подвійного rerun від Streamlit
    login_button_html = f"""
    <a href="{auth_url}" target="_top" style="
        background-color: #5865F2;
        color: white;
        padding: 15px 25px;
        text-align: center;
        text-decoration: none;
        display: inline-block;
        border-radius: 8px;
        font-weight: bold;
        font-family: sans-serif;
    ">🔑 Увійти через Discord</a>
    """
    st.markdown(login_button_html, unsafe_allow_html=True)

if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

# --- 4. ІНТЕРФЕЙС (ПІСЛЯ ВХОДУ) ---
user = st.session_state.auth_user
st.sidebar.title(f"👤 {user['username']}")
menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.auth_user = None
    st.query_params.clear()
    st.rerun()

# Функція OCR (Tesseract)
def ocr_process(image_np, is_id=False):
    gray = cv2.cvtColor(image_np, cv2.COLOR_RGB2GRAY)
    _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    txt = pytesseract.image_to_string(thresh, config='--psm 7')
    if is_id: return "".join(re.findall(r'\d+', txt))
    return re.sub(r'[^a-zA-Zа-яА-ЯіїєґІЇЄҐ]', '', txt).capitalize()

# --- ЛОГІКА СКАНЕРА ТА НАЛАШТУВАНЬ ТУТ (як у попередній версії) ---
if menu == "⚙️ Налаштування":
    st.header("📐 Трафарет")
    f = st.file_uploader("Зразок", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Зона", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        if st.button("💾 Зберегти"):
            # Збереження в БД
            coords = {"Surname": None, "Name": None, "ID": None}
            # (тут код завантаження/оновлення з БД)
            st.success("Збережено!")

elif menu == "📄 Сканер":
    st.header("📸 Сканер паспортів")
    p_files = st.file_uploader("Завантажте фото", accept_multiple_files=True, type=['png','jpg','jpeg'])
    if p_files and st.button("🔍 Почати"):
        st.info("Розпізнавання активовано...")
        # (код OCR тут)
