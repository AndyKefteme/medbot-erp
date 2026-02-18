import streamlit as st
import cv2
import numpy as np
import pytesseract
import requests
import json
import sqlite3
import re
import os
import io
import shutil # Додано для пошуку програм
from PIL import Image
from streamlit_cropper import st_cropper
from datetime import datetime
from requests_oauthlib import OAuth2Session

# --- 0. СИСТЕМНА ПЕРЕВІРКА (LOGGING) ---
def log(msg):
    print(f"[SYSTEM_LOG] {msg}")

log("Ініціалізація...")

# Автоматичний пошук Tesseract в Linux
tess_path = shutil.which("tesseract")
if tess_path:
    pytesseract.pytesseract.tesseract_cmd = tess_path
    log(f"Tesseract знайдено: {tess_path}")
else:
    log("КРИТИЧНА ПОМИЛКА: Tesseract не знайдено в системі!")

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# --- 1. КОНФІГУРАЦІЯ SECRETS ---
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
    log("Конфігурація завантажена успішно.")
except Exception as e:
    log(f"Помилка конфігурації: {str(e)}")
    st.error("Налаштуйте Secrets у Streamlit Cloud!")
    st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro")

# --- 2. БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 3. АВТОРИЗАЦІЯ (БЕЗ ЦИКЛІВ) ---
if 'auth_user' not in st.session_state:
    st.session_state.auth_user = None

def handle_discord_login():
    # 1. Перевірка повернення з Discord
    if "code" in st.query_params:
        code = st.query_params["code"]
        log("Обробка коду авторизації...")
        try:
            discord = OAuth2Session(config['DISCORD_CLIENT_ID'], 
                                    redirect_uri=config['DISCORD_REDIRECT_URI'], 
                                    scope=["identify", "guilds", "guilds.members.read"])
            token = discord.fetch_token('https://discord.com/api/oauth2/token', 
                                        client_secret=config['DISCORD_CLIENT_SECRET'], 
                                        code=code)
            
            u_data = discord.get('https://discord.com/api/users/@me').json()
            # Отримання ролей
            m_res = discord.get(f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member")
            if m_res.status_code == 200:
                m_data = m_res.json()
                u_roles = m_data.get('roles', [])
                is_adm = config['ADMIN_ROLE_ID'] in u_roles
                if config['ALLOWED_ROLE_ID'] in u_roles or is_adm:
                    st.session_state.auth_user = {"id": u_data['id'], "username": u_data['username'], "is_admin": is_adm}
                    st.query_params.clear()
                    st.rerun()
                else:
                    st.error("❌ У вас немає доступу до цієї системи.")
            else:
                st.error("❌ Ви не є учасником потрібного сервера Discord.")
        except Exception as e:
            log(f"OAuth Error: {e}")
            st.error(f"Помилка входу. Спробуйте ще раз.")
        st.stop()

    # 2. Екран входу
    st.title("🏥 MedBot ERP System")
    auth_url = (f"https://discord.com/api/oauth2/authorize?client_id={config['DISCORD_CLIENT_ID']}&"
                f"redirect_uri={requests.utils.quote(config['DISCORD_REDIRECT_URI'])}&"
                f"response_type=code&scope=identify%20guilds%20guilds.members.read")

    st.markdown(f'''
        <a href="{auth_url}" target="_top" style="
            background-color: #5865F2; color: white; padding: 15px 30px; 
            text-decoration: none; border-radius: 8px; font-weight: bold; 
            display: inline-block; font-size: 1.2em;
        ">🔑 Увійти через Discord</a>
    ''', unsafe_allow_html=True)

if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

# --- 4. ІНТЕРФЕЙС ПІСЛЯ ВХОДУ ---
user = st.session_state.auth_user
st.sidebar.title(f"👤 {user['username']}")
if st.sidebar.button("🚪 Вихід"):
    st.session_state.auth_user = None
    st.rerun()

st.success(f"Вітаємо, {user['username']}! Система готова до роботи.")
# Тут іде решта вашого коду для сканера...
