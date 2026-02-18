import streamlit as st
import cv2
import numpy as np
import pytesseract
import requests
import json
import sqlite3
import re
import os
from PIL import Image
from requests_oauthlib import OAuth2Session

# --- 0. НАЛАШТУВАННЯ ТА ЛОГУВАННЯ ---
print("[BOOT] Запуск системи...")

# Вимикаємо перевірку HTTPS для OAuth (потрібно для Streamlit Cloud)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Шлях до Tesseract в Linux
if os.path.exists('/usr/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

# --- 1. ПЕРЕВІРКА SECRETS ---
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
    print("[BOOT] Secrets завантажено.")
except Exception as e:
    st.error(f"❌ Помилка Secrets: {e}")
    st.stop()

st.set_page_config(page_title="MedBot Pro", layout="wide")

# --- 2. БАЗА ДАНИХ ---
db_path = "medbot.db"
conn = sqlite3.connect(db_path, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, count INTEGER, date TEXT)')
conn.commit()

# --- 3. АВТОРИЗАЦІЯ ---
if 'user' not in st.session_state:
    st.session_state.user = None

def login():
    st.title("🏥 MedBot ERP")
    
    # Обробка повернення з Discord
    if "code" in st.query_params:
        try:
            discord = OAuth2Session(config['DISCORD_CLIENT_ID'], redirect_uri=config['DISCORD_REDIRECT_URI'], scope=['identify', 'guilds.members.read'])
            token = discord.fetch_token('https://discord.com/api/oauth2/token', client_secret=config['DISCORD_CLIENT_SECRET'], code=st.query_params["code"])
            user_data = discord.get('https://discord.com/api/users/@me').json()
            
            # Перевірка ролі (спрощена)
            st.session_state.user = user_data['username']
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Помилка входу: {e}")

    # Кнопка входу
    auth_url = f"https://discord.com/api/oauth2/authorize?client_id={config['DISCORD_CLIENT_ID']}&redirect_uri={requests.utils.quote(config['DISCORD_REDIRECT_URI'])}&response_type=code&scope=identify%20guilds.members.read"
    
    st.markdown(f'<a href="{auth_url}" target="_top" style="background:#5865F2;color:white;padding:10px 20px;border-radius:5px;text-decoration:none;">🔑 Увійти через Discord</a>', unsafe_allow_html=True)

if not st.session_state.user:
    login()
    st.stop()

# --- 4. ГОЛОВНИЙ ЕКРАН ---
st.sidebar.write(f"👤 {st.session_state.user}")
if st.sidebar.button("Вихід"):
    st.session_state.user = None
    st.rerun()

st.success("Ви увійшли!")
st.write("Тепер можна додавати функції сканування.")
