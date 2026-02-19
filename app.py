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
from requests_oauthlib import OAuth2Session

# --- КОНФІГУРАЦІЯ ---
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

# --- ДОПОМІЖНІ ФУНКЦІЇ ---
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

# --- СТАН СЕСІЇ ---
if 'auth_user' not in st.session_state:
    st.session_state.auth_user = None
if 'oauth_state' not in st.session_state:
    st.session_state.oauth_state = None
if 'scanned_data' not in st.session_state:
    st.session_state.scanned_data = []

# --- АВТОРИЗАЦІЯ DISCORD (ВИПРАВЛЕНО) ---
def handle_discord_login():
    # 1. Отримуємо код з URL за новим API
    code = st.query_params.get("code")

    # 2. Якщо коду немає і юзер не в системі — створюємо посилання
    if not code and st.session_state.auth_user is None:
        discord = OAuth2Session(
            config['DISCORD_CLIENT_ID'],
            redirect_uri=config['DISCORD_REDIRECT_URI'],
            scope=["identify", "guilds", "guilds.members.read"]
        )
        auth_url, state = discord.authorization_url("https://discord.com/api/oauth2/authorize")
        
        # Зберігаємо state для перевірки при поверненні
        st.session_state.oauth_state = state
        
        st.title("🏥 MedBot ERP System")
        st.write("Для початку роботи необхідно авторизуватися:")
        
        # Використовуємо офіційну кнопку Streamlit для зовнішніх посилань
        st.link_button("🔑 УВІЙТИ ЧЕРЕЗ DISCORD", auth_url, type="primary")
        st.stop()

    # 3. Якщо код повернувся — обмінюємо його на токен
    if code and st.session_state.auth_user is None:
        try:
            discord = OAuth2Session(
                config['DISCORD_CLIENT_ID'],
                redirect_uri=config['DISCORD_REDIRECT_URI'],
                state=st.session_state.oauth_state
            )
            
            token = discord.fetch_token(
                "https://discord.com/api/oauth2/token",
                client_secret=config['DISCORD_CLIENT_SECRET'],
                code=code
            )
            
            user_data = discord.get("https://discord.com/api/users/@me").json()
            
            # Перевірка ролей на сервері
            m_url = f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member"
            m_res = discord.get(m_url)
            
            if m_res.status_code == 200:
                m_data = m_res.json()
                u_roles = m_data.get('roles', [])
                is_adm = config['ADMIN_ROLE_ID'] in u_roles
                is_allowed = config['ALLOWED_ROLE_ID'] in u_roles or is_adm
                
                if is_allowed:
                    st.session_state.auth_user = {
                        "id": user_data["id"], 
                        "username": user_data["username"], 
                        "is_admin": is_adm
                    }
                    st.query_params.clear()
                    st.rerun()
                else:
                    st.error("🚫 Доступ заборонено: у вас немає потрібної ролі.")
                    st.stop()
            else:
                st.error("❌ Ви не є учасником Discord сервера.")
                st.stop()
                
        except Exception as e:
            st.error(f"Помилка OAuth: {e}")
            if st.button("Спробувати ще раз"):
                st.query_params.clear()
                st.rerun()
            st.stop()

# Запуск логіки входу
handle_discord_login()

# --- ОСНОВНИЙ ІНТЕРФЕЙС (ЯКИЙ ТИ СТВОРИВ) ---
user = st.session_state.auth_user
current_coords = load_user_coords(user['id'])

st.sidebar.title(f"👤 {user['username']}")
menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.auth_user = None
    st.rerun()

elif menu == "📊 Адмін-панель":
    if not user['is_admin']:
        st.warning("Доступ заборонено.")
    else:
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
                st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Налаштування трафарету")
    f = st.file_uploader("Завантажте зразок паспорта", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Оберіть поле для виділення", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        if st.button("💾 Зберегти зону"):
            new_c = current_coords
            new_c[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_user_coords(user['id'], new_c)
            st.success(f"Зону {target} збережено!")

elif menu == "📄 Сканер":
    if not all(current_coords.values()):
        st.warning("⚠️ Спочатку налаштуйте зони в 'Налаштуваннях'!")
    else:
        st.header("📸 Сканування документів")
        p_files = st.file_uploader("Паспорти", accept_multiple_files=True, type=['png','jpg','jpeg'])
        if p_files and st.button("🔍 Почати OCR"):
            results = []
            for f in p_files:
                img_np = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                item_data = {}
                for lbl, (x, y, w, h) in current_coords.items():
                    crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                    txt = " ".join([t[1] for t in reader.readtext(crop)])
                    item_data[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else txt.strip().capitalize()
                results.append(item_data)
            st.session_state.scanned_data = results
            st.rerun()

        if st.session_state.scanned_data:
            st.subheader("📝 Перевірка та відправка")
            final_list = []
            for idx, item in enumerate(st.session_state.scanned_data):
                c1, c2, c3 = st.columns(3)
                s = c1.text_input(f"Прізвище #{idx}", item['Surname'], key=f"s{idx}")
                n = c2.text_input(f"Ім'я #{idx}", item['Name'], key=f"n{idx}")
                u = c3.text_input(f"ID #{idx}", item['ID'], key=f"u{idx}")
                final_list.append({"Surname": s, "Name": n, "ID": u})
            
            if st.button("🚀 ВІДПРАВИТИ В DISCORD"):
                msg = f"🏥 **Новий звіт** від <@{user['id']}>\n" + "\n".join([f"• {r['Surname']} {r['Name']} (ID: {r['ID']})" for r in final_list])
                requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": msg})
                st.success("Звіт надіслано!")
                st.session_state.scanned_data = []
