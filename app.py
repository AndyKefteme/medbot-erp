import streamlit as st
import numpy as np
import easyocr
import requests
import json
import sqlite3
import re
import os
from PIL import Image
from streamlit_cropper import st_cropper
from datetime import datetime
from urllib.parse import quote

# --- ЗАВАНТАЖЕННЯ КОНФІГУРАЦІЇ ---
def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

config = load_config()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- OCR (КЕШУВАННЯ) ---
@st.cache_resource
def load_reader():
    return easyocr.Reader(['en', 'uk'], gpu=False)

reader = load_reader()

# --- СТОРІНКА АВТОРИЗАЦІЇ ---
def login_page():
    client_id = str(config['DISCORD_CLIENT_ID']).strip()
    redirect_uri = str(config['DISCORD_REDIRECT_URI']).strip()
    
    # Формуємо URL вручну. БЕЗ зайвих бібліотек.
    # ВАЖЛИВО: scope має бути розділений через %20 (пробіл)
    auth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
        f"&response_type=code"
        f"&scope=identify%20guilds%20guilds.members.read"
    )

    st.title("🏥 MedBot ERP System")
    st.divider()
    
    col1, col2 = st.columns([2, 1])
    with col1:
        st.subheader("Вхід")
        st.markdown(f'''
            <a href="{auth_url}" target="_self" style="
                background-color: #5865F2; color: white; padding: 18px 45px; 
                text-decoration: none; border-radius: 10px; font-weight: bold; font-size: 22px;
                display: inline-block;
            ">🔑 УВІЙТИ ЧЕРЕЗ DISCORD</a>
        ''', unsafe_allow_html=True)
        st.write(f"Конфігурація: `{redirect_uri}`")

    # ОБРОБКА CALLBACK
    if "code" in st.query_params:
        code = st.query_params["code"]
        
        # Обмін коду на токен через POST запит
        token_url = "https://discord.com/api/oauth2/token"
        data = {
            'client_id': client_id,
            'client_secret': config['DISCORD_CLIENT_SECRET'],
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_uri
        }
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        
        res = requests.post(token_url, data=data, headers=headers)
        
        if res.status_code == 200:
            access_token = res.json()['access_token']
            
            # Отримання даних користувача
            user_headers = {"Authorization": f"Bearer {access_token}"}
            user_info = requests.get("https://discord.com/api/users/@me", headers=user_headers).json()
            
            # Перевірка ролі на сервері
            guild_id = config['GUILD_ID']
            member_res = requests.get(f"https://discord.com/api/users/@me/guilds/{guild_id}/member", headers=user_headers)
            
            if member_res.status_code == 200:
                member_data = member_res.json()
                roles = member_data.get('roles', [])
                is_admin = config['ADMIN_ROLE_ID'] in roles
                is_allowed = config['ALLOWED_ROLE_ID'] in roles or is_admin
                
                if is_allowed:
                    st.session_state.auth_user = {"id": user_info['id'], "username": user_info['username'], "is_admin": is_admin}
                    st.query_params.clear()
                    st.rerun()
                else:
                    st.error("🚫 У вас немає доступу (відсутня роль).")
            else:
                st.error("❌ Ви не на сервері.")
        else:
            st.error(f"Помилка Discord: {res.text}")

if 'auth_user' not in st.session_state:
    login_page()
    st.stop()

# --- ОСНОВНА ЧАСТИНА (ПІСЛЯ ВХОДУ) ---
user = st.session_state.auth_user

def get_coords(u_id):
    res = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    return json.loads(res[0]) if res else {"Surname": None, "Name": None, "ID": None}

u_coords = get_coords(user['id'])

st.sidebar.title(f"👤 {user['username']}")
menu = st.sidebar.radio("Меню", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.clear()
    st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Налаштування зон")
    f = st.file_uploader("Завантажте зразок", type=['jpg', 'png'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Поле", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='blue', return_type='box')
        if st.button("Зберегти"):
            u_coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (user['id'], json.dumps(u_coords)))
            conn.commit()
            st.success("Збережено!")

elif menu == "📄 Сканер":
    if not all(u_coords.values()):
        st.warning("Налаштуйте зони.")
    else:
        st.header("📸 Сканування")
        files = st.file_uploader("Фото", accept_multiple_files=True)
        if files and st.button("🔍 Розпізнати"):
            scanned = []
            for f in files:
                img = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                data = {}
                for lbl, (x, y, w, h) in u_coords.items():
                    crop = img[int(y):int(y+h), int(x):int(x+w)]
                    txt = " ".join([t[1] for t in reader.readtext(crop)])
                    data[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else txt.strip().capitalize()
                scanned.append(data)
            st.session_state.results = scanned
            st.rerun()

        if 'results' in st.session_state:
            final = []
            for i, r in enumerate(st.session_state.results):
                c1, c2, c3 = st.columns(3)
                s = c1.text_input(f"Прізвище {i}", r['Surname'], key=f"s{i}")
                n = c2.text_input(f"Ім'я {i}", r['Name'], key=f"n{i}")
                u = c3.text_input(f"ID {i}", r['ID'], key=f"u{i}")
                final.append({"Surname": s, "Name": n, "ID": u})
            
            if st.button("🚀 ВІДПРАВИТИ В DISCORD"):
                msg = f"🏥 **Звіт від** <@{user['id']}>\n" + "\n".join([f"• {x['Surname']} {x['Name']} ID:{x['ID']}" for x in final])
                requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": msg})
                st.success("Надіслано!")
                del st.session_state.results
