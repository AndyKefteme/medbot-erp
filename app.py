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
from PIL import Image
from streamlit_cropper import st_cropper
from datetime import datetime
from requests_oauthlib import OAuth2Session

# --- НАЛАШТУВАННЯ ---
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

try:
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
except FileNotFoundError:
    st.error("Помилка: Файл config.json не знайдено!")
    st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

@st.cache_resource
def load_ocr():
    return easyocr.Reader(['en'], gpu=False)

reader = load_ocr()

# --- ФУНКЦІЇ ---
def save_user_coords(u_id, coords):
    cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (u_id, json.dumps(coords)))
    conn.commit()

def load_user_coords(u_id):
    if not u_id: return {"Surname": None, "Name": None, "ID": None}
    saved = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    if saved: return json.loads(saved[0])
    return {"Surname": None, "Name": None, "ID": None}

def compress_image(image_file):
    img = Image.open(image_file).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    buf.seek(0)
    return buf

# --- СТАН СЕСІЇ ---
if 'auth_user' not in st.session_state:
    st.session_state.auth_user = None
if 'scanned_data' not in st.session_state:
    st.session_state.scanned_data = []

# --- АВТОРИЗАЦІЯ (ОБХІД БЛОКУВАННЯ) ---
def handle_discord_login():
    # Твоє робоче посилання
    auth_url = "https://discord.com/api/oauth2/authorize?response_type=code&client_id=1473419565978615929&redirect_uri=https%3A%2F%2Fems-zvit.streamlit.app&scope=identify+guilds+guilds.members.read&state=edKLeYvUD7lV7nbkhdvRKfNAxcWKpZ"
    
    qp = st.query_params
    
    if "code" not in qp and st.session_state.auth_user is None:
        # Цей скрипт ігнорує фрейми Streamlit і перенаправляє головне вікно браузера
        st.markdown("### Вхід до системи...")
        st.components.v1.html(f"""
            <script>
                window.top.location.href = "{auth_url}";
            </script>
        """, height=0)
        # Додаткова кнопка, якщо JS заблоковано в самому браузері
        st.markdown(f'<a href="{auth_url}" target="_top" style="color: white; background: #5865F2; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Натисніть тут, якщо не перекинуло автоматично</a>', unsafe_allow_html=True)
        st.stop()
    
    if "code" in qp and st.session_state.auth_user is None:
        try:
            discord = OAuth2Session(config['DISCORD_CLIENT_ID'], redirect_uri=config['DISCORD_REDIRECT_URI'])
            token = discord.fetch_token('https://discord.com/api/oauth2/token', 
                                        client_secret=config['DISCORD_CLIENT_SECRET'], 
                                        code=qp['code'])
            
            u_data = discord.get('https://discord.com/api/users/@me').json()
            
            # Перевірка ролей
            m_url = f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member"
            m_res = discord.get(m_url)
            
            if m_res.status_code == 200:
                m_data = m_res.json()
                u_roles = m_data.get('roles', [])
                is_adm = config['ADMIN_ROLE_ID'] in u_roles
                is_allowed = config['ALLOWED_ROLE_ID'] in u_roles or is_adm
                
                if is_allowed:
                    st.session_state.auth_user = {"id": u_data['id'], "username": u_data['username'], "is_admin": is_adm}
                    st.query_params.clear()
                    st.rerun()
                else:
                    st.error("🚫 Немає потрібної ролі.")
                    st.stop()
            else:
                st.error("❌ Ви не на сервері.")
                st.stop()
        except Exception as e:
            st.error(f"Помилка: {e}")
            st.stop()

# Запуск
handle_discord_login()

if st.session_state.auth_user:
    user = st.session_state.auth_user
    current_coords = load_user_coords(user['id'])

    st.sidebar.title(f"👤 {user['username']}")
    menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

    if menu == "🚪 Вихід":
        st.session_state.auth_user = None
        st.rerun()

    elif menu == "⚙️ Налаштування":
        st.header("📐 Трафарет")
        f = st.file_uploader("Завантажте зразок", type=['png','jpg','jpeg'])
        if f:
            img = Image.open(f).convert("RGB").resize((1920, 1080))
            target = st.selectbox("Зона", ["Surname", "Name", "ID"])
            rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
            if st.button("💾 Зберегти"):
                new_c = current_coords
                new_c[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
                save_user_coords(user['id'], new_c)
                st.success("Збережено!")

    elif menu == "📄 Сканер":
        if not all(current_coords.values()):
            st.warning("⚠️ Налаштуйте координати!")
        else:
            st.header("📸 Новий звіт")
            p_files = st.file_uploader("Паспорти", accept_multiple_files=True, type=['png','jpg','jpeg'])
            if p_files and st.button("🔍 Розпізнати"):
                st.session_state.scanned_data = []
                for f in p_files:
                    img_np = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                    res = {}
                    for lbl, (x, y, w, h) in current_coords.items():
                        crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                        txt = " ".join([t[1] for t in reader.readtext(crop)])
                        res[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else txt.strip().capitalize()
                    st.session_state.scanned_data.append(res)
                st.rerun()

            if st.session_state.scanned_data:
                final = []
                for idx, item in enumerate(st.session_state.scanned_data):
                    cols = st.columns(3)
                    s = cols[0].text_input(f"Прізвище #{idx}", item['Surname'], key=f"s{idx}")
                    n = cols[1].text_input(f"Ім'я #{idx}", item['Name'], key=f"n{idx}")
                    u = cols[2].text_input(f"ID #{idx}", item['ID'], key=f"u{idx}")
                    final.append({"Surname": s, "Name": n, "ID": u})
                
                if st.button("🚀 ВІДПРАВИТИ"):
                    msg = f"🏥 **ЗВІТ** від <@{user['id']}>\n" + "\n".join([f"{r['Surname']} {r['Name']} #{r['ID']}" for r in final])
                    requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": msg})
                    st.success("Надіслано!")
                    st.session_state.scanned_data = []
