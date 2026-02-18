import streamlit as st
import numpy as np
import easyocr
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
from urllib.parse import quote

# --- 1. ЗАВАНТАЖЕННЯ КОНФІГУРАЦІЇ ---
def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        st.error(f"Критична помилка: Не вдалося прочитати config.json ({e})")
        st.stop()

config = load_config()

# Шлях для моделей OCR
MODEL_DIR = os.path.join(os.getcwd(), "ocr_models")
if not os.path.exists(MODEL_DIR):
    os.makedirs(MODEL_DIR)

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- 2. БАЗА ДАНИХ (SQLite) ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 3. OCR МОДУЛЬ (EasyOCR) ---
@st.cache_resource(show_spinner=False)
def get_ocr_reader():
    return easyocr.Reader(['en', 'uk'], gpu=False, model_storage_directory=MODEL_DIR)

# --- 4. ЛОГІКА АВТОРИЗАЦІЇ DISCORD ---
def show_login():
    client_id = str(config['DISCORD_CLIENT_ID']).strip()
    redirect_uri = str(config['DISCORD_REDIRECT_URI']).strip()
    
    # Пряме посилання. Важливо: scope через %20
    auth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
        f"&response_type=code"
        f"&scope=identify%20guilds%20guilds.members.read"
    )

    st.title("🏥 MedBot ERP System")
    st.markdown("---")
    
    col1, col2 = st.columns([2, 1])
    with col1:
        st.subheader("Вхід в систему")
        st.info(f"Redirect URI встановлено як: `{redirect_uri}`")
        
        # Кнопка для входу
        st.markdown(f'''
            <a href="{auth_url}" target="_self" style="
                background-color: #5865F2; color: white; padding: 20px 50px; 
                text-decoration: none; border-radius: 12px; font-weight: bold; font-size: 24px;
                display: inline-block; box-shadow: 0 4px 15px rgba(0,0,0,0.3);
            ">🔑 УВІЙТИ ЧЕРЕЗ DISCORD</a>
        ''', unsafe_allow_html=True)
        
        st.markdown("---")
        st.write("Якщо кнопка не працює, скопіюйте це посилання:")
        st.code(auth_url)

    with col2:
        st.subheader("Статус")
        if 'reader' not in st.session_state:
            with st.spinner("Завантаження ШІ..."):
                st.session_state.reader = get_ocr_reader()
            st.success("✅ OCR Готовий")
            st.rerun()
        else:
            st.success("✅ OCR Активний")

    # Обробка повернення з Discord
    if "code" in st.query_params:
        code = st.query_params["code"]
        token_data = {
            'client_id': client_id,
            'client_secret': config['DISCORD_CLIENT_SECRET'],
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_uri
        }
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        
        # Обмін коду на токен
        res = requests.post("https://discord.com/api/oauth2/token", data=token_data, headers=headers)
        
        if res.status_code == 200:
            token = res.json()['access_token']
            user_headers = {"Authorization": f"Bearer {token}"}
            
            # Дані юзера
            u_info = requests.get("https://discord.com/api/users/@me", headers=user_headers).json()
            
            # Перевірка ролі
            g_id = config['GUILD_ID']
            m_res = requests.get(f"https://discord.com/api/users/@me/guilds/{g_id}/member", headers=user_headers)
            
            if m_res.status_code == 200:
                m_data = m_res.json()
                roles = m_data.get('roles', [])
                is_admin = config['ADMIN_ROLE_ID'] in roles
                is_allowed = config['ALLOWED_ROLE_ID'] in roles or is_admin
                
                if is_allowed:
                    st.session_state.auth_user = {"id": u_info['id'], "username": u_info['username'], "is_admin": is_admin}
                    st.query_params.clear()
                    st.rerun()
                else:
                    st.error("🚫 Немає доступу: Відсутня роль на сервері.")
            else:
                st.error("❌ Ви не є учасником сервера Discord.")
        else:
            st.error(f"Помилка Discord: {res.text}")

# Перевірка сесії
if 'auth_user' not in st.session_state:
    show_login()
    st.stop()

# --- 5. ОСНОВНИЙ РОБОЧИЙ ІНТЕРФЕЙС ---
user = st.session_state.auth_user
reader = st.session_state.reader

def get_coords(u_id):
    res = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    return json.loads(res[0]) if res else {"Surname": None, "Name": None, "ID": None}

u_coords = get_coords(user['id'])

st.sidebar.title(f"👤 {user['username']}")
page = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Логи", "🚪 Вихід"])

if page == "🚪 Вихід":
    st.session_state.clear()
    st.rerun()

elif page == "⚙️ Налаштування":
    st.header("📐 Налаштування зон розпізнавання")
    f = st.file_uploader("Завантажте фото-зразок", type=['jpg', 'png', 'jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Яке поле налаштовуємо?", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='blue', return_type='box')
        if st.button("💾 Зберегти координати"):
            u_coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (user['id'], json.dumps(u_coords)))
            conn.commit()
            st.success(f"Зону для {target} збережено!")

elif page == "📄 Сканер":
    if not all(u_coords.values()):
        st.warning("⚠️ Спочатку перейдіть в 'Налаштування' та виділіть зони на паспорті.")
    else:
        st.header("📸 Сканування документів")
        files = st.file_uploader("Завантажте фото (можна декілька)", accept_multiple_files=True)
        if files and st.button("🔍 Почати розпізнавання"):
            results = []
            p_bar = st.progress(0)
            for i, f in enumerate(files):
                img_np = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                item = {}
                for lbl, (x, y, w, h) in u_coords.items():
                    crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                    txt_data = reader.readtext(crop)
                    txt = " ".join([t[1] for t in txt_data])
                    item[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else txt.strip().capitalize()
                results.append(item)
                p_bar.progress((i + 1) / len(files))
            st.session_state.scanned_data = results
            st.rerun()

        if 'scanned_data' in st.session_state:
            st.subheader("📝 Перевірка даних")
            final_to_send = []
            for i, res in enumerate(st.session_state.scanned_data):
                c1, c2, c3 = st.columns(3)
                s = c1.text_input(f"Прізвище #{i}", res['Surname'], key=f"s{i}")
                n = c2.text_input(f"Ім'я #{i}", res['Name'], key=f"n{i}")
                u = c3.text_input(f"ID #{i}", res['ID'], key=f"u{i}")
                final_to_send.append({"Surname": s, "Name": n, "ID": u})
            
            if st.button("🚀 ВІДПРАВИТИ ЗВІТ У DISCORD"):
                msg = f"🏥 **Новий звіт від** <@{user['id']}>\n" + "\n".join([f"• {x['Surname']} {x['Name']} (ID: {x['ID']})" for x in final_to_send])
                requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": msg})
                cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final_to_send), datetime.now().strftime("%d.%m.%Y %H:%M")))
                conn.commit()
                st.success("✅ Звіт успішно надіслано!")
                del st.session_state.scanned_data

elif page == "📊 Логи":
    if user['is_admin']:
        st.header("📊 Журнал активності")
        data = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 50").fetchall()
        st.table([{"Користувач": r[1], "К-сть записів": r[2], "Дата/Час": r[3]} for r in data])
    else:
        st.error("У вас немає прав адміністратора для перегляду логів.")
