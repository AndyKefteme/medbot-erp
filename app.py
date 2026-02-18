import streamlit as st
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
from urllib.parse import quote

# --- ЗАВАНТАЖЕННЯ КОНФІГУРАЦІЇ ---
try:
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
except Exception as e:
    st.error(f"Помилка конфігурації: {e}")
    st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- OCR МОДЕЛЬ ---
@st.cache_resource
def load_reader():
    return easyocr.Reader(['en', 'uk'], gpu=False)

reader = load_reader()

# --- АВТОРИЗАЦІЯ ---
def login_page():
    client_id = config['DISCORD_CLIENT_ID']
    redirect_uri = config['DISCORD_REDIRECT_URI']
    
    # Генеруємо посилання вручну, щоб уникнути помилок бібліотек
    scope = quote("identify guilds guilds.members.read")
    auth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={quote(redirect_uri)}"
        f"&response_type=code"
        f"&scope={scope}"
    )

    st.title("🏥 MedBot ERP System")
    st.divider()
    
    col1, _ = st.columns([2, 1])
    with col1:
        st.info("👋 Авторизуйтесь для доступу до системи.")
        # Використовуємо кнопку з target="_self" для прямого переходу
        st.markdown(f'''
            <a href="{auth_url}" target="_self" style="
                background-color: #5865F2; color: white; padding: 15px 35px; 
                text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 20px;
                display: inline-block;
            ">🔑 УВІЙТИ ЧЕРЕЗ DISCORD</a>
        ''', unsafe_allow_html=True)

    # Обробка повернення з кодом
    if "code" in st.query_params:
        code = st.query_params["code"]
        token_data = {
            'client_id': client_id,
            'client_secret': config['DISCORD_CLIENT_SECRET'],
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_uri
        }
        
        # 1. Отримуємо токен
        r = requests.post("https://discord.com/api/oauth2/token", data=token_data)
        if r.status_code == 200:
            token = r.json()['access_token']
            headers = {"Authorization": f"Bearer {token}"}
            
            # 2. Отримуємо дані юзера
            u_info = requests.get("https://discord.com/api/users/@me", headers=headers).json()
            
            # 3. Перевіряємо роль на сервері
            g_id = config['GUILD_ID']
            m_resp = requests.get(f"https://discord.com/api/users/@me/guilds/{g_id}/member", headers=headers)
            
            if m_resp.status_code == 200:
                member_data = m_resp.json()
                roles = member_data.get('roles', [])
                is_admin = config['ADMIN_ROLE_ID'] in roles
                is_allowed = config['ALLOWED_ROLE_ID'] in roles or is_admin
                
                if is_allowed:
                    st.session_state.auth_user = {"id": u_info['id'], "username": u_info['username'], "is_admin": is_admin}
                    st.query_params.clear()
                    st.rerun()
                else:
                    st.error("🚫 Доступ заборонено: відсутня роль.")
            else:
                st.error("❌ Ви не є учасником потрібного сервера.")
        else:
            st.error(f"Помилка Discord: {r.text}")

# Запуск логіки
if 'auth_user' not in st.session_state:
    login_page()
    st.stop()

# --- ПІСЛЯ ВХОДУ ---
user = st.session_state.auth_user

def get_coords(u_id):
    res = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    return json.loads(res[0]) if res else {"Surname": None, "Name": None, "ID": None}

u_coords = get_coords(user['id'])

st.sidebar.title(f"👤 {user['username']}")
page = st.sidebar.radio("Меню", ["📄 Сканер", "⚙️ Налаштування", "📊 Логи", "🚪 Вихід"])

if page == "🚪 Вихід":
    st.session_state.clear()
    st.rerun()

elif page == "⚙️ Налаштування":
    st.header("📐 Калібрування")
    f = st.file_uploader("Завантажити зразок", type=['jpg', 'png', 'jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Поле", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='blue', return_type='box')
        if st.button("Зберегти зону"):
            u_coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (user['id'], json.dumps(u_coords)))
            conn.commit()
            st.success("Збережено!")

elif page == "📄 Сканер":
    if not all(u_coords.values()):
        st.warning("Спочатку налаштуйте зони.")
    else:
        st.header("📸 Сканування паспортів")
        files = st.file_uploader("Виберіть фото", accept_multiple_files=True)
        if files and st.button("🔍 Почати"):
            res = []
            for f in files:
                img = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                data = {}
                for lbl, (x, y, w, h) in u_coords.items():
                    crop = img[int(y):int(y+h), int(x):int(x+w)]
                    txt = " ".join([t[1] for t in reader.readtext(crop)])
                    data[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else txt.strip().capitalize()
                res.append(data)
            st.session_state.scanned = res
            st.rerun()

        if 'scanned' in st.session_state:
            final = []
            for i, r in enumerate(st.session_state.scanned):
                c1, c2, c3 = st.columns(3)
                s = c1.text_input(f"Прізвище {i}", r['Surname'], key=f"s{i}")
                n = c2.text_input(f"Ім'я {i}", r['Name'], key=f"n{i}")
                u = c3.text_input(f"ID {i}", r['ID'], key=f"u{i}")
                final.append({"Surname": s, "Name": n, "ID": u})
            
            if st.button("🚀 ВІДПРАВИТИ ЗВІТ"):
                msg = f"🏥 **Звіт від** <@{user['id']}>\n" + "\n".join([f"• {x['Surname']} {x['Name']} (ID: {x['ID']})" for x in final])
                requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": msg})
                cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final), datetime.now().strftime("%Y-%m-%d %H:%M")))
                conn.commit()
                st.success("Успішно надіслано!")
                del st.session_state.scanned

elif page == "📊 Логи":
    if user['is_admin']:
        st.subheader("Журнал")
        logs = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 30").fetchall()
        st.table([{"Юзер": r[1], "К-сть": r[2], "Час": r[3]} for r in logs])
