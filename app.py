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
from PIL import Image
from streamlit_cropper import st_cropper
from datetime import datetime
from requests_oauthlib import OAuth2Session

# --- 0. АДАПТАЦІЯ ПІД LINUX/STREAMLIT ---
# Вказуємо шлях до Tesseract (має бути встановлений через packages.txt)
if os.path.exists('/usr/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# --- 1. КОНФІГУРАЦІЯ (SECRETS) ---
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

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- 2. БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 3. OCR (ШВИДКИЙ TESSERACT) ---
def ocr_process(image_np, is_id=False):
    gray = cv2.cvtColor(image_np, cv2.COLOR_RGB2GRAY)
    _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    txt = pytesseract.image_to_string(thresh, config='--psm 7')
    if is_id: return "".join(re.findall(r'\d+', txt))
    return re.sub(r'[^a-zA-Zа-яА-ЯіїєґІЇЄҐ]', '', txt).capitalize()

def load_user_coords(u_id):
    saved = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    return json.loads(saved[0]) if saved else {"Surname": None, "Name": None, "ID": None}

def save_user_coords(u_id, coords):
    cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (u_id, json.dumps(coords)))
    conn.commit()

# --- 4. АВТОРИЗАЦІЯ (МЕТОД LINUX-BREAKOUT) ---
if 'auth_user' not in st.session_state:
    st.session_state.auth_user = None

def handle_discord_login():
    client_id = config['DISCORD_CLIENT_ID']
    redirect_uri = config['DISCORD_REDIRECT_URI']
    scope = "identify guilds guilds.members.read"
    auth_url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&redirect_uri={requests.utils.quote(redirect_uri)}&response_type=code&scope={requests.utils.quote(scope)}"
    
    st.title("🏥 MedBot ERP System")
    st.info("Будь ласка, увійдіть через Discord.")

    # Ця кнопка використовує JS для переходу, що працює на Linux
    if st.button("🔑 Увійти через Discord", type="primary"):
        js = f"window.top.location.href = '{auth_url}';"
        st.components.v1.html(f"<script>{js}</script>", height=0)
        st.stop()

    qp = st.query_params
    if "code" in qp:
        try:
            discord = OAuth2Session(client_id, redirect_uri=redirect_uri, scope=scope.split())
            token = discord.fetch_token('https://discord.com/api/oauth2/token', client_secret=config['DISCORD_CLIENT_SECRET'], code=qp['code'])
            u_data = discord.get('https://discord.com/api/users/@me').json()
            m_data = discord.get(f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member").json()
            u_roles = m_data.get('roles', [])
            is_adm = config['ADMIN_ROLE_ID'] in u_roles
            if config['ALLOWED_ROLE_ID'] in u_roles or is_adm:
                st.session_state.auth_user = {"id": u_data['id'], "username": u_data['username'], "is_admin": is_adm}
                st.query_params.clear()
                st.rerun()
        except Exception as e:
            st.error(f"Помилка: {e}")

if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

# --- 5. ОСНОВНИЙ ІНТЕРФЕЙС (ВАШ ВІЗУАЛ) ---
user = st.session_state.auth_user
st.sidebar.title(f"👤 {user['username']}")
menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмінка", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.auth_user = None
    st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Трафарет")
    f = st.file_uploader("Зразок", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Зона", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        if st.button("💾 Зберегти"):
            coords = load_user_coords(user['id'])
            coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_user_coords(user['id'], coords)
            st.success("Збережено!")

elif menu == "📄 Сканер":
    coords = load_user_coords(user['id'])
    if not all(coords.values()):
        st.warning("Налаштуйте трафарет!")
    else:
        st.header("📸 Сканер")
        p_files = st.file_uploader("Паспорти", accept_multiple_files=True, type=['png','jpg','jpeg'])
        if p_files and st.button("🔍 Старт"):
            results = []
            for f in p_files:
                img = Image.open(f).convert("RGB").resize((1920, 1080))
                img_np = np.array(img)
                res = {}
                for lbl, (x, y, w, h) in coords.items():
                    crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                    res[lbl] = ocr_process(crop, is_id=(lbl=="ID"))
                results.append(res)
            st.session_state.scanned_data = results
            st.rerun()

        if st.session_state.get('scanned_data'):
            for idx, item in enumerate(st.session_state.scanned_data):
                cols = st.columns(3)
                item['Surname'] = cols[0].text_input(f"Прізвище {idx}", item['Surname'])
                item['Name'] = cols[1].text_input(f"Ім'я {idx}", item['Name'])
                item['ID'] = cols[2].text_input(f"ID {idx}", item['ID'])
            
            if st.button("🚀 Відправити"):
                msg = f"🏥 Звіт від <@{user['id']}>\n" + "\n".join([f"• {r['Surname']} {r['Name']} ({r['ID']})" for r in st.session_state.scanned_data])
                requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": msg})
                st.success("Надіслано!")
                st.session_state.scanned_data = []

elif menu == "📊 Адмінка" and user['is_admin']:
    st.header("📊 Статистика")
    logs = cursor.execute("SELECT * FROM logs").fetchall()
    st.table(logs)
