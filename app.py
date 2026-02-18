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

# --- 0. НАЛАШТУВАННЯ TESSERACT ДЛЯ LINUX (STREAMLIT CLOUD) ---
if os.path.exists('/usr/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

# Дозволяємо OAuth працювати через проксі
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
except Exception:
    st.error("❌ Помилка: Налаштуйте 'Secrets' у Streamlit Cloud!")
    st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- 2. БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 3. ФУНКЦІЇ РОЗПІЗНАВАННЯ (Tesseract замість EasyOCR) ---
def ocr_process(image_np):
    gray = cv2.cvtColor(image_np, cv2.COLOR_RGB2GRAY)
    _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    text = pytesseract.image_to_string(thresh, config='--psm 7')
    return text.strip()

def save_user_coords(u_id, coords):
    cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (u_id, json.dumps(coords)))
    conn.commit()

def load_user_coords(u_id):
    saved = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    if saved: return json.loads(saved[0])
    return {"Surname": None, "Name": None, "ID": None}

def compress_image(image_file):
    img = Image.open(image_file).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    buf.seek(0)
    return buf

# --- 4. СТАН СЕСІЇ ---
if 'auth_user' not in st.session_state: st.session_state.auth_user = None
if 'scanned_data' not in st.session_state: st.session_state.scanned_data = []
if 'passport_payload' not in st.session_state: st.session_state.passport_payload = []
if 'file_uploader_key' not in st.session_state: st.session_state.file_uploader_key = 0

# --- 5. АВТОРИЗАЦІЯ DISCORD ---
def handle_discord_login():
    client_id = config['DISCORD_CLIENT_ID']
    redirect_uri = config['DISCORD_REDIRECT_URI']
    scope = "identify guilds guilds.members.read"
    auth_url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&scope={scope}"
    
    st.title("🏥 MedBot ERP System")
    # Покращена кнопка з target="_top" для обходу блокувань
    login_html = f'''
        <a href="{auth_url}" target="_top" style="
            background-color: #5865F2; color: white; padding: 15px 30px; 
            text-decoration: none; border-radius: 8px; font-weight: bold; 
            display: inline-block; font-size: 18px;
        ">🔑 Увійти через Discord</a>
    '''
    st.markdown(login_html, unsafe_allow_html=True)
    
    qp = st.query_params
    if "code" in qp:
        try:
            discord = OAuth2Session(client_id, redirect_uri=redirect_uri, scope=scope.split())
            token = discord.fetch_token('https://discord.com/api/oauth2/token', client_secret=config['DISCORD_CLIENT_SECRET'], code=qp['code'])
            u_data = discord.get('https://discord.com/api/users/@me').json()
            
            # Перевірка ролей
            m_data = discord.get(f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member").json()
            u_roles = m_data.get('roles', [])
            is_adm = config['ADMIN_ROLE_ID'] in u_roles
            is_allowed = config['ALLOWED_ROLE_ID'] in u_roles or is_adm
            
            if is_allowed:
                st.session_state.auth_user = {"id": u_data['id'], "username": u_data['username'], "is_admin": is_adm}
                st.query_params.clear()
                st.rerun()
            else:
                st.error("❌ У вас немає доступу.")
        except Exception as e:
            st.error(f"Помилка входу: {e}")

# Перевірка авторизації
if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

user = st.session_state.auth_user

# --- 6. ГОЛОВНЕ МЕНЮ (Ваш старий стиль) ---
st.sidebar.title(f"👤 {user['username']}")
if user['is_admin']: st.sidebar.subheader("👑 Адміністратор")
else: st.sidebar.caption("🩺 Співробітник")

menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.auth_user = None
    st.rerun()

elif menu == "📊 Адмін-панель" and user['is_admin']:
    st.header("🛡 Управління")
    t_logs, t_ban = st.tabs(["📝 Логи", "🚫 Бан"])
    with t_logs:
        h = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 50").fetchall()
        st.table([{"Користувач": r[1], "К-сть": r[2], "Дата": r[3]} for r in h])
    with t_ban:
        bid = st.text_input("Discord ID для бану")
        if st.button("🚫 Бан"):
            cursor.execute("INSERT OR IGNORE INTO blacklist VALUES (?)", (bid,))
            conn.commit()
            st.success(f"ID {bid} заблоковано!")

elif menu == "⚙️ Налаштування":
    st.header("📐 Трафарет")
    f = st.file_uploader("Завантажте зразок", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Зона", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        if st.button("💾 Зберегти"):
            coords = load_user_coords(user['id'])
            coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_user_coords(user['id'], coords)
            st.success(f"Зону {target} збережено!")

elif menu == "📄 Сканер":
    current_coords = load_user_coords(user['id'])
    if not all(current_coords.values()):
        st.warning("⚠️ Налаштуйте координати в 'Налаштуваннях'!")
    else:
        st.header("📸 Новий звіт")
        p_files = st.file_uploader("1. Паспорти", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"p_{st.session_state.file_uploader_key}")
        if p_files and st.button("🔍 Розпізнати"):
            st.session_state.scanned_data = []
            st.session_state.passport_payload = []
            for i, f in enumerate(p_files):
                img_pil = Image.open(f).convert("RGB").resize((1920, 1080))
                img_np = np.array(img_pil)
                res = {}
                for lbl, (x, y, w, h) in current_coords.items():
                    crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                    res[lbl] = ocr_process(crop)
                st.session_state.scanned_data.append(res)
                buf = compress_image(f)
                st.session_state.passport_payload.append((f"p{i}", (f"p_{i}.jpg", buf.read(), "image/jpeg")))
            st.rerun()

        if st.session_state.scanned_data:
            st.subheader("📝 Перевірка")
            final = []
            for idx, item in enumerate(st.session_state.scanned_data):
                cols = st.columns([3, 3, 2])
                s = cols[0].text_input(f"Прізвище #{idx+1}", item['Surname'], key=f"s_{idx}")
                n = cols[1].text_input(f"Ім'я #{idx+1}", item['Name'], key=f"n_{idx}")
                u = cols[2].text_input(f"ID #{idx+1}", item['ID'], key=f"u_{idx}")
                final.append({"Surname": s, "Name": n, "ID": u})
            
            c_files = st.file_uploader("2. Докази розрахунку", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"c_{st.session_state.file_uploader_key}")
            if st.button("🚀 ВІДПРАВИТИ ЗВІТ"):
                if not c_files: st.error("Додайте докази!")
                else:
                    msg = f"🏥 **НОВИЙ МЕД-ЗВІТ**\n<@{user['id']}> | {user['username']}\n\n" + \
                          "\n".join([f"• {r['Surname']} {r['Name']} (ID: {r['ID']})" for r in final])
                    requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": msg}, files=st.session_state.passport_payload)
                    c_pay = []
                    for i, cf in enumerate(c_files):
                        c_pay.append((f"c{i}", (f"c_{i}.jpg", compress_image(cf).read(), "image/jpeg")))
                    requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": "💳 **Докази:**"}, files=c_pay)
                    cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final), datetime.now().strftime("%d.%m.%Y %H:%M")))
                    conn.commit()
                    st.success("✅ Звіт надіслано!")
                    st.session_state.scanned_data = []
                    st.session_state.file_uploader_key += 1
                    st.rerun()
