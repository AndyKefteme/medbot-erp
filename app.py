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

# --- АВТОРИЗАЦІЯ (ВИПРАВЛЕНИЙ ПЕРЕХІД) ---
def handle_discord_login():
    # Твоє пряме посилання
    auth_url = "https://discord.com/api/oauth2/authorize?response_type=code&client_id=1473419565978615929&redirect_uri=https%3A%2F%2Fems-zvit.streamlit.app&scope=identify+guilds+guilds.members.read&state=edKLeYvUD7lV7nbkhdvRKfNAxcWKpZ"
    
    qp = st.query_params
    
    # Якщо користувач не авторизований і в URL немає коду
    if "code" not in qp and st.session_state.auth_user is None:
        st.title("🏥 MedBot ERP System")
        st.markdown("### Необхідна авторизація")
        
        # ВИРІШАЛЬНИЙ МОМЕНТ: target="_top" виводить посилання ЗА МЕЖІ фрейму Streamlit
        st.markdown(f"""
            <div style="text-align: center; margin-top: 50px;">
                <p style="font-size: 1.2rem;">Натисніть кнопку нижче, щоб увійти через Discord:</p>
                <a href="{auth_url}" target="_top" style="
                    background-color: #5865F2;
                    color: white;
                    padding: 18px 40px;
                    text-decoration: none;
                    border-radius: 10px;
                    font-weight: bold;
                    font-size: 20px;
                    display: inline-block;
                    box-shadow: 0 4px 15px rgba(0,0,0,0.3);
                ">🔑 УВІЙТИ В СИСТЕМУ</a>
            </div>
        """, unsafe_allow_html=True)
        st.stop()
    
    # Якщо код прийшов у параметрах
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
                    st.error("🚫 Доступ заборонено (відсутня роль).")
                    st.stop()
            else:
                st.error("❌ Ви не є учасником Discord сервера.")
                st.stop()
        except Exception as e:
            st.error(f"Помилка авторизації: {e}")
            st.stop()

# Старт логіки
handle_discord_login()

# --- ОСНОВНИЙ КОД (ПІСЛЯ ВХОДУ) ---
user = st.session_state.auth_user
current_coords = load_user_coords(user['id'])

st.sidebar.title(f"👤 {user['username']}")
menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.auth_user = None
    st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Налаштування трафарету")
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
        st.warning("⚠️ Налаштуйте координати в 'Налаштуваннях'!")
    else:
        st.header("📸 Новий звіт")
        p_files = st.file_uploader("Завантажте паспорти", accept_multiple_files=True, type=['png','jpg','jpeg'])
        if p_files and st.button("🔍 Розпізнати"):
            scanned = []
            for f in p_files:
                img_np = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                res = {}
                for lbl, (x, y, w, h) in current_coords.items():
                    crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                    txt = " ".join([t[1] for t in reader.readtext(crop)])
                    res[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else txt.strip().capitalize()
                scanned.append(res)
            st.session_state.scanned_data = scanned
            st.rerun()

        if st.get('scanned_data' in st.session_state and st.session_state.scanned_data):
            final = []
            for idx, item in enumerate(st.session_state.scanned_data):
                cols = st.columns(3)
                s = cols[0].text_input(f"Прізвище #{idx}", item['Surname'], key=f"s{idx}")
                n = cols[1].text_input(f"Ім'я #{idx}", item['Name'], key=f"n{idx}")
                u = cols[2].text_input(f"ID #{idx}", item['ID'], key=f"u{idx}")
                final.append({"Surname": s, "Name": n, "ID": u})
            
            if st.button("🚀 ВІДПРАВИТИ ЗВІТ"):
                msg = f"🏥 **ЗВІТ** від <@{user['id']}>\n" + "\n".join([f"• {r['Surname']} {r['Name']} #{r['ID']}" for r in final])
                requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": msg})
                st.success("Надіслано!")
                st.session_state.scanned_data = []
