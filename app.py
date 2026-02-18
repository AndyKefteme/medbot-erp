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
from PIL import Image
from streamlit_cropper import st_cropper
from datetime import datetime
from requests_oauthlib import OAuth2Session

# --- 1. ВИПРАВЛЕННЯ СЕРВЕРНИХ ПОМИЛОК ---
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Створюємо папку для моделей, щоб вони не качалися щоразу в нікуди
MODEL_DIR = os.path.join(os.getcwd(), "ocr_models")
if not os.path.exists(MODEL_DIR):
    os.makedirs(MODEL_PATH)

# --- 2. ПЕРЕВІРКА SECRETS ---
if "discord" not in st.secrets:
    st.error("Критична помилка: Додайте [discord] секцію в Secrets на Streamlit Cloud!")
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

# --- 4. ОПТИМІЗОВАНИЙ OCR ---
@st.cache_resource(show_spinner=False)
def load_reader():
    # Відображаємо статус тільки під час першого завантаження
    with st.spinner("🏥 Активуємо медичний сканер... Це займе хвилину."):
        return easyocr.Reader(['en', 'uk'], gpu=False, model_storage_directory=MODEL_DIR)

# --- 5. АВТОРИЗАЦІЯ ---
def login_page():
    # Використовуємо посилання з Secrets
    redirect_uri = config['DISCORD_REDIRECT_URI']
    scope = ['identify', 'guilds', 'guilds.members.read']
    
    discord = OAuth2Session(config['DISCORD_CLIENT_ID'], redirect_uri=redirect_uri, scope=scope)
    auth_url, _ = discord.authorization_url('https://discord.com/api/oauth2/authorize')

    st.title("🏥 MedBot ERP System")
    st.markdown("---")
    
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.info("Будь ласка, увійдіть через Discord для доступу до бази даних.")
        # Важливо: target="_self" змушує відкрити в тому ж вікні, уникаючи блокувань
        st.markdown(f'''
            <div style="text-align: center;">
                <a href="{auth_url}" target="_self" style="
                    background-color: #5865F2; color: white; padding: 20px 40px; 
                    text-decoration: none; border-radius: 12px; font-weight: bold; font-size: 22px;
                    display: inline-block; box-shadow: 0 4px 15px rgba(0,0,0,0.2);
                ">🔑 УВІЙТИ ЧЕРЕЗ DISCORD</a>
            </div>
        ''', unsafe_allow_html=True)

    # Перевірка повернення коду
    params = st.query_params
    if "code" in params:
        try:
            token = discord.fetch_token('https://discord.com/api/oauth2/token',
                                        client_secret=config['DISCORD_CLIENT_SECRET'],
                                        code=params["code"])
            user_data = discord.get('https://discord.com/api/users/@me').json()
            
            # Перевірка ролей
            member_url = f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member"
            member_data = discord.get(member_url).json()
            roles = member_data.get('roles', [])
            
            is_admin = config['ADMIN_ROLE_ID'] in roles
            is_allowed = config['ALLOWED_ROLE_ID'] in roles or is_admin
            
            if not is_allowed:
                st.error("🚫 Доступ заборонено: у вас немає потрібної ролі.")
                return

            st.session_state.auth_user = {"id": user_data['id'], "username": user_data['username'], "is_admin": is_admin}
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Помилка авторизації: {e}")

# Перевірка логіну
if 'auth_user' not in st.session_state:
    login_page()
    st.stop()

# Головна логіка після логіну
reader = load_reader()
user = st.session_state.auth_user

# --- 6. МЕНЮ ТА ФУНКЦІЇ ---
st.sidebar.title(f"👤 {user['username']}")
page = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

def get_coords(u_id):
    res = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    return json.loads(res[0]) if res else {"Surname": None, "Name": None, "ID": None}

def save_coords(u_id, data):
    cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (u_id, json.dumps(data)))
    conn.commit()

user_c = get_coords(user['id'])

if page == "🚪 Вихід":
    st.session_state.auth_user = None
    st.rerun()

elif page == "⚙️ Налаштування":
    st.header("📐 Налаштування зон сканування")
    f = st.file_uploader("Зразок документа", type=['jpg', 'png'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Виберіть поле", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='blue', return_type='box')
        if st.button("💾 Зберегти"):
            user_c[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_coords(user['id'], user_c)
            st.success(f"Зону {target} оновлено!")

elif page == "📄 Сканер":
    if not all(user_c.values()):
        st.warning("Спочатку налаштуйте зони в 'Налаштуваннях'")
    else:
        st.header("📸 Сканування паспортів")
        files = st.file_uploader("Завантажте фото", accept_multiple_files=True, type=['jpg', 'png'])
        if files and st.button("🔍 Почати розпізнавання"):
            results = []
            for f in files:
                img = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                item = {}
                for lbl, (x, y, w, h) in user_c.items():
                    crop = img[int(y):int(y+h), int(x):int(x+w)]
                    txt = " ".join([t[1] for t in reader.readtext(crop)])
                    item[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else txt.strip().capitalize()
                results.append(item)
            st.session_state.current_scan = results
            st.rerun()

        if 'current_scan' in st.session_state:
            st.subheader("📝 Перевірка даних")
            final_list = []
            for i, res in enumerate(st.session_state.current_scan):
                c1, c2, c3 = st.columns(3)
                s = c1.text_input(f"Прізвище #{i+1}", res['Surname'], key=f"s{i}")
                n = c2.text_input(f"Ім'я #{i+1}", res['Name'], key=f"n{i}")
                u = c3.text_input(f"ID #{i+1}", res['ID'], key=f"u{i}")
                final_list.append({"Surname": s, "Name": n, "ID": u})
            
            if st.button("🚀 ВІДПРАВИТИ В DISCORD"):
                msg = f"🏥 **ЗВІТ** від {user['username']}\n" + "\n".join([f"• {x['Surname']} {x['Name']} (ID: {x['ID']})" for x in final_list])
                requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": msg})
                cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final_list), datetime.now().strftime("%Y-%m-%d %H:%M")))
                conn.commit()
                st.success("Надіслано!")
                del st.session_state.current_scan

elif page == "📊 Адмін-панель":
    if user['is_admin']:
        st.header("📊 Останні звіти")
        logs = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 30").fetchall()
        st.table([{"Юзер": r[1], "Кількість": r[2], "Час": r[3]} for r in logs])
    else:
        st.error("Доступ заборонено.")
