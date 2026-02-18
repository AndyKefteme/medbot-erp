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

# --- 0. ТЕХНІЧНА АДАПТАЦІЯ (ДЛЯ LINUX) ---
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

if os.path.exists('/usr/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

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
    st.error(f"❌ Помилка Secrets: {e}")
    st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- 2. БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 3. ДОПОМІЖНІ ФУНКЦІЇ ---
def ocr_process(image_np, is_id=False):
    gray = cv2.cvtColor(image_np, cv2.COLOR_RGB2GRAY)
    _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    txt = pytesseract.image_to_string(thresh, config='--psm 7')
    if is_id: return "".join(re.findall(r'\d+', txt))
    return re.sub(r'[^a-zA-Zа-яА-ЯіїєґІЇЄҐ]', '', txt).capitalize()

def load_user_coords(u_id):
    cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,))
    saved = cursor.fetchone()
    return json.loads(saved[0]) if saved else {"Surname": None, "Name": None, "ID": None}

def save_user_coords(u_id, coords):
    cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (u_id, json.dumps(coords)))
    conn.commit()

def compress_image(image_file):
    img = Image.open(image_file).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    buf.seek(0)
    return buf

# --- 4. АВТОРИЗАЦІЯ (ДИЗАЙНЕРСЬКА КНОПКА) ---
if 'auth_user' not in st.session_state: st.session_state.auth_user = None
if 'scanned_data' not in st.session_state: st.session_state.scanned_data = []
if 'passport_payload' not in st.session_state: st.session_state.passport_payload = []
if 'file_uploader_key' not in st.session_state: st.session_state.file_uploader_key = 0

def handle_discord_login():
    if "code" in st.query_params:
        try:
            discord = OAuth2Session(config['DISCORD_CLIENT_ID'], redirect_uri=config['DISCORD_REDIRECT_URI'], scope=['identify', 'guilds', 'guilds.members.read'])
            token = discord.fetch_token('https://discord.com/api/oauth2/token', client_secret=config['DISCORD_CLIENT_SECRET'], code=st.query_params["code"])
            u_data = discord.get('https://discord.com/api/users/@me').json()
            m_data = discord.get(f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member").json()
            u_roles = m_data.get('roles', [])
            is_adm = config['ADMIN_ROLE_ID'] in u_roles
            if config['ALLOWED_ROLE_ID'] in u_roles or is_adm:
                st.session_state.auth_user = {"id": u_data['id'], "username": u_data['username'], "is_admin": is_adm}
                st.query_params.clear()
                st.rerun()
        except Exception: pass

    st.markdown("<h1 style='text-align: center;'>🏥 MedBot ERP System</h1>", unsafe_allow_html=True)
    auth_url = f"https://discord.com/api/oauth2/authorize?client_id={config['DISCORD_CLIENT_ID']}&redirect_uri={requests.utils.quote(config['DISCORD_REDIRECT_URI'])}&response_type=code&scope=identify%20guilds%20guilds.members.read"
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown(f'''
            <div style="text-align: center; margin-top: 50px;">
                <a href="{auth_url}" target="_top" style="background-color: #5865F2; color: white; padding: 20px 40px; text-decoration: none; border-radius: 10px; font-weight: bold; font-size: 20px; display: inline-block;">🔑 УВІЙТИ ЧЕРЕЗ DISCORD</a>
            </div>
        ''', unsafe_allow_html=True)

if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

# --- 5. ГОЛОВНЕ МЕНЮ ТА ОФОРМЛЕННЯ ---
user = st.session_state.auth_user
st.sidebar.title(f"👤 {user['username']}")
if user['is_admin']: st.sidebar.subheader("👑 Адміністратор")
else: st.sidebar.caption("🩺 Співробітник")

menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.auth_user = None
    st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Налаштування трафарету")
    if st.button("🗑 Скинути зони"):
        save_user_coords(user['id'], {"Surname": None, "Name": None, "ID": None})
        st.rerun()
        
    f = st.file_uploader("Зразок", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Виберіть поле", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        if st.button("💾 Зберегти зону"):
            coords = load_user_coords(user['id'])
            coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_user_coords(user['id'], coords)
            st.success(f"Зону {target} збережено!")

elif menu == "📄 Сканер":
    curr_coords = load_user_coords(user['id'])
    if not all(curr_coords.values()):
        st.warning("⚠️ Налаштуйте трафарет!")
    else:
        st.header("📸 Масове сканування")
        p_files = st.file_uploader("1. Завантажте паспорти", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"p_{st.session_state.file_uploader_key}")
        
        if p_files and st.button("🔍 Почати OCR"):
            st.session_state.scanned_data = []
            st.session_state.passport_payload = []
            for i, f in enumerate(p_files):
                img = Image.open(f).convert("RGB").resize((1920, 1080))
                img_np = np.array(img)
                res = {}
                for lbl, (x, y, w, h) in curr_coords.items():
                    crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                    res[lbl] = ocr_process(crop, is_id=(lbl=="ID"))
                st.session_state.scanned_data.append(res)
                st.session_state.passport_payload.append((f"p{i}", (f"p_{i}.jpg", compress_image(f).read(), "image/jpeg")))
            st.rerun()

        if st.session_state.scanned_data:
            st.subheader("📝 Редагування")
            for idx, item in enumerate(st.session_state.scanned_data):
                cols = st.columns(3)
                item['Surname'] = cols[0].text_input(f"Прізвище #{idx+1}", item['Surname'], key=f"s{idx}")
                item['Name'] = cols[1].text_input(f"Ім'я #{idx+1}", item['Name'], key=f"n{idx}")
                item['ID'] = cols[2].text_input(f"ID #{idx+1}", item['ID'], key=f"i{idx}")
            
            c_files = st.file_uploader("2. Докази (чеки)", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"c_{st.session_state.file_uploader_key}")
            
            if st.button("🚀 ВІДПРАВИТИ ЗВІТ"):
                if not c_files: st.error("Додайте чеки!")
                else:
                    report = f"🏥 **НОВИЙ ЗВІТ**\nВід: <@{user['id']}>\n\n" + \
                             "\n".join([f"• {r['Surname']} {r['Name']} (ID: {r['ID']})" for r in st.session_state.scanned_data])
                    requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": report}, files=st.session_state.passport_payload)
                    
                    c_pay = []
                    for i, cf in enumerate(c_files):
                        c_pay.append((f"c{i}", (f"c_{i}.jpg", compress_image(cf).read(), "image/jpeg")))
                    requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": "💳 **Чеки:**"}, files=c_pay)
                    
                    cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(st.session_state.scanned_data), datetime.now().strftime("%d.%m.%Y %H:%M")))
                    conn.commit()
                    st.success("✅ Надіслано!")
                    st.session_state.scanned_data = []
                    st.session_state.file_uploader_key += 1
                    st.rerun()

elif menu == "📊 Адмін-панель" and user['is_admin']:
    st.header("🛡 Адмінка")
    logs = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC").fetchall()
    st.table([{"Користувач": r[1], "К-сть": r[2], "Дата": r[3]} for r in logs])
