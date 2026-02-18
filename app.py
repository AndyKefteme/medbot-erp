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
from requests_oauthlib import OAuth2Session

# Функція для миттєвого виводу в консоль Streamlit Cloud
def log_to_console(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] [MEDBOT-LOG] {msg}")
    sys.stdout.flush()

log_to_console("--- ЗАПУСК ДОДАТКА ---")

# --- 1. СИСТЕМНІ НАЛАШТУВАННЯ ---
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
MODEL_DIR = os.path.join(os.getcwd(), "ocr_models")
if not os.path.exists(MODEL_DIR):
    os.makedirs(MODEL_DIR)
    log_to_console(f"Створено папку для моделей: {MODEL_DIR}")

# --- 2. ПЕРЕВІРКА SECRETS ---
if "discord" not in st.secrets:
    log_to_console("ПОМИЛКА: Secrets не знайдено!")
    st.error("Налаштуйте Secrets!")
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
log_to_console("База даних готова.")

# --- 4. ЗАВАНТАЖЕННЯ OCR З ЛОГУВАННЯМ ---
@st.cache_resource(show_spinner=False)
def load_reader():
    log_to_console("ПОЧАТОК завантаження моделей OCR (easyocr)...")
    with st.spinner("🏥 Завантаження ШІ-модулів (це може тривати 2-3 хв)..."):
        # Завантажуємо англійську та українську
        reader = easyocr.Reader(['en', 'uk'], gpu=False, model_storage_directory=MODEL_DIR)
        log_to_console("OCR МОДЕЛІ ЗАВАНТАЖЕНО УСПІШНО.")
    return reader

# --- 5. СТАН OCR ТА АВТОРИЗАЦІЯ ---
# Перевіряємо статус OCR для відображення юзеру
if 'ocr_ready' not in st.session_state:
    st.session_state.ocr_ready = False

def login_page():
    log_to_console("Відображення сторінки логіну.")
    redirect_uri = config['DISCORD_REDIRECT_URI'].strip()
    client_id = config['DISCORD_CLIENT_ID'].strip()
    
    scope = "identify guilds guilds.members.read"
    auth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={requests.utils.quote(redirect_uri)}"
        f"&response_type=code"
        f"&scope={requests.utils.quote(scope)}"
    )

    st.title("🏥 MedBot ERP System")
    st.markdown("---")
    
    col1, col2 = st.columns([2, 1])
    
    with col1:
        st.info("👋 Вітаємо! Будь ласка, авторизуйтесь через Discord.")
        st.markdown(f'''
            <div style="margin-top: 20px;">
                <a href="{auth_url}" target="_self" style="
                    background-color: #5865F2; color: white; padding: 16px 42px; 
                    text-decoration: none; border-radius: 12px; font-weight: bold; font-size: 22px;
                    display: inline-block; box-shadow: 0 6px 20px rgba(88,101,242,0.4);
                ">🔑 УВІЙТИ ЧЕРЕЗ DISCORD</a>
            </div>
        ''', unsafe_allow_html=True)

    with col2:
        st.subheader("Статус системи")
        if not st.session_state.ocr_ready:
            st.warning("⏳ Очікування ініціалізації OCR...")
            # Запускаємо завантаження
            try:
                st.session_state.reader_obj = load_reader()
                st.session_state.ocr_ready = True
                log_to_console("Статус: OCR Ready")
                st.rerun()
            except Exception as e:
                log_to_console(f"Критична помилка OCR: {e}")
                st.error("Помилка завантаження ШІ.")
        else:
            st.success("✅ OCR активовано")
            st.success("✅ База даних підключена")

    # Обробка коду Discord
    params = st.query_params
    if "code" in params:
        log_to_console(f"Отримано код авторизації: {params['code'][:5]}***")
        try:
            discord = OAuth2Session(client_id, redirect_uri=redirect_uri, scope=scope.split())
            token = discord.fetch_token(
                'https://discord.com/api/oauth2/token',
                client_secret=config['DISCORD_CLIENT_SECRET'].strip(),
                code=params["code"]
            )
            user_data = discord.get('https://discord.com/api/users/@me').json()
            u_id = user_data['id']
            log_to_console(f"Юзер {user_data['username']} ({u_id}) намагається увійти.")

            member_url = f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member"
            member_resp = discord.get(member_url)
            
            if member_resp.status_code != 200:
                log_to_console("Помилка: Юзера не знайдено на сервері Discord.")
                st.error("Ви не є учасником потрібного сервера!")
                return

            member_data = member_resp.json()
            roles = member_data.get('roles', [])
            is_admin = config['ADMIN_ROLE_ID'] in roles
            is_allowed = config['ALLOWED_ROLE_ID'] in roles or is_admin
            
            if not is_allowed:
                log_to_console(f"Доступ відхилено для {user_data['username']} (немає ролі).")
                st.error("🚫 У вас немає доступу.")
                return

            st.session_state.auth_user = {"id": u_id, "username": user_data['username'], "is_admin": is_admin}
            log_to_console(f"Успішний вхід: {user_data['username']}")
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            log_to_console(f"Помилка OAuth: {e}")
            st.error(f"Помилка входу: {e}")

# Перевірка авторизації
if 'auth_user' not in st.session_state:
    login_page()
    st.stop()

# --- 6. ОСНОВНИЙ ІНТЕРФЕЙС (коротко) ---
reader = st.session_state.reader_obj
user = st.session_state.auth_user

st.sidebar.title(f"👤 {user['username']}")
st.write(f"### Вітаємо, {user['username']}! Система готова до роботи.")
# Тут іде решта вашого коду сканера...
