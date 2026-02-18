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
import sys
from PIL import Image
from streamlit_cropper import st_cropper
from datetime import datetime
from requests_oauthlib import OAuth2Session

# --- МАКСИМАЛЬНЕ ЛОГУВАННЯ ---
def log(msg):
    print(f"[MEDBOT_LOG] {msg}")
    # Не використовуємо st.write тут, щоб не ламати інтерфейс до ініціалізації

log("Запуск додатка...")

# --- 0. ПЕРЕВІРКА TESSERACT ---
log("Перевірка Tesseract...")
tess_path = '/usr/bin/tesseract'
if os.path.exists(tess_path):
    pytesseract.pytesseract.tesseract_cmd = tess_path
    log(f"Tesseract знайдено за шляхом: {tess_path}")
else:
    log("Tesseract НЕ знайдено за стандартним шляхом Linux!")

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# --- 1. КОНФІГУРАЦІЯ ---
log("Завантаження конфігурації...")
try:
    # Пріоритет на Secrets (Streamlit Cloud)
    if "DISCORD_CLIENT_ID" in st.secrets:
        log("Використовуємо Streamlit Secrets")
        config = {
            "DISCORD_CLIENT_ID": st.secrets["DISCORD_CLIENT_ID"],
            "DISCORD_CLIENT_SECRET": st.secrets["DISCORD_CLIENT_SECRET"],
            "DISCORD_REDIRECT_URI": st.secrets["DISCORD_REDIRECT_URI"],
            "GUILD_ID": st.secrets["GUILD_ID"],
            "ADMIN_ROLE_ID": st.secrets["ADMIN_ROLE_ID"],
            "ALLOWED_ROLE_ID": st.secrets["ALLOWED_ROLE_ID"],
            "DISCORD_WEBHOOK_URL": st.secrets["DISCORD_WEBHOOK_URL"]
        }
    else:
        log("Secrets не знайдено, шукаємо config.json...")
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
except Exception as e:
    log(f"КРИТИЧНА ПОМИЛКА КОНФІГУРАЦІЇ: {str(e)}")
    st.error(f"Помилка конфігурації: {e}")
    st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- 2. БАЗА ДАНИХ ---
log("Підключення до БД...")
try:
    DB_PATH = os.path.join(os.getcwd(), "medbot_db.sqlite")
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
    cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
    cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
    conn.commit()
    log("БД готова.")
except Exception as e:
    log(f"ПОМИЛКА БД: {str(e)}")
    st.error(f"БД: {e}")

# --- 3. АВТОРИЗАЦІЯ ---
if 'auth_user' not in st.session_state:
    st.session_state.auth_user = None

def handle_discord_login():
    log("Відображення вікна входу...")
    client_id = config['DISCORD_CLIENT_ID']
    redirect_uri = config['DISCORD_REDIRECT_URI']
    scope = "identify guilds guilds.members.read"
    
    auth_url = (f"https://discord.com/api/oauth2/authorize?client_id={client_id}&"
                f"redirect_uri={requests.utils.quote(redirect_uri)}&"
                f"response_type=code&scope={requests.utils.quote(scope)}")
    
    st.title("🏥 MedBot ERP System")
    
    if st.button("🔑 Увійти через Discord", type="primary"):
        log("Натиснуто кнопку входу, запуск JS-переходу...")
        components.html(f"<script>window.top.location.href = '{auth_url}';</script>", height=0)
        st.stop()

    # Перевірка вхідних параметрів (OAuth Code)
    if "code" in st.query_params:
        code = st.query_params["code"]
        log(f"Отримано код авторизації: {code[:5]}***")
        try:
            discord = OAuth2Session(client_id, redirect_uri=redirect_uri, scope=scope.split())
            token = discord.fetch_token('https://discord.com/api/oauth2/token', 
                                        client_secret=config['DISCORD_CLIENT_SECRET'], 
                                        code=code)
            log("Токен отримано.")
            
            u_data = discord.get('https://discord.com/api/users/@me').json()
            log(f"Користувач: {u_data.get('username')}")
            
            m_data = discord.get(f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member").json()
            
            u_roles = m_data.get('roles', [])
            is_adm = config['ADMIN_ROLE_ID'] in u_roles
            is_allowed = config['ALLOWED_ROLE_ID'] in u_roles or is_adm
            
            if is_allowed:
                log("Доступ дозволено.")
                st.session_state.auth_user = {"id": u_data['id'], "username": u_data['username'], "is_admin": is_adm}
                st.query_params.clear()
                st.rerun()
            else:
                log("ДОСТУП ЗАБОРОНЕНО (Ролі не знайдено)")
                st.error("У вас немає потрібної ролі в Discord.")
        except Exception as e:
            log(f"ПОМИЛКА OAUTH: {str(e)}")
            st.error(f"Помилка входу: {e}")

# Перевірка стану
if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

# --- 4. ІНТЕРФЕЙС (СПРОЩЕНИЙ ДЛЯ ТЕСТУ) ---
user = st.session_state.auth_user
log(f"Інтерфейс завантажено для {user['username']}")
st.sidebar.success(f"Ви ввійшли як {user['username']}")

if st.sidebar.button("🚪 Вийти"):
    st.session_state.auth_user = None
    st.rerun()

st.write("🎉 Ви успішно авторизовані! Виберіть розділ у меню зліва.")
