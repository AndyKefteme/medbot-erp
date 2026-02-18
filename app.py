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

# --- 1. УНІВЕРСАЛЬНА КОНФІГУРАЦІЯ ---
# Дозволяємо HTTP для локальної розробки
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

if os.path.exists("config.json"):
    # Для локального запуску (використовуємо файл)
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
else:
    # Для Streamlit Cloud (використовуємо Secrets)
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
        st.error("❌ Конфігурація не знайдена! Перевірте налаштування 'Secrets' у Streamlit Cloud.")
        st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- 2. БАЗА ДАНИХ (SQLite) ---
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

# --- 3. ДОПОМІЖНІ ФУНКЦІЇ ---
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
if 'auth_user' not in st.session_state:
    st.session_state.auth_user = None
if 'scanned_data' not in st.session_state:
    st.session_state.scanned_data = []
if 'passport_payload' not in st.session_state:
    st.session_state.passport_payload = []
if 'file_uploader_key' not in st.session_state:
    st.session_state.file_uploader_key = 0

# --- 5. АВТОРИЗАЦІЯ ---
def handle_discord_login():
    scope = ['identify', 'guilds', 'guilds.members.read']
    discord = OAuth2Session(config['DISCORD_CLIENT_ID'], redirect_uri=config['DISCORD_REDIRECT_URI'], scope=scope)
    auth_url, _ = discord.authorization_url('https://discord.com/api/oauth2/authorize')
    
    st.title("🏥 MedBot ERP System")
    st.write("Для початку роботи необхідно авторизуватися через ваш Discord аккаунт.")
    
    # Використовуємо HTML-кнопку з target="_top" для примусового переходу
    login_html = f'''
        <a href="{auth_url}" target="_top" style="
            background-color: #5865F2;
            color: white;
            padding: 15px 30px;
            text-decoration: none;
            border-radius: 8px;
            font-weight: bold;
            font-size: 18px;
            display: inline-block;
            text-align: center;
            margin-top: 20px;
        ">🔑 Увійти через Discord</a>
    '''
    st.markdown(login_html, unsafe_allow_html=True)
    
    qp = st.query_params
    if "code" in qp:
        try:
            token = discord.fetch_token('https://discord.com/api/oauth2/token', client_secret=config['DISCORD_CLIENT_SECRET'], code=qp['code'])
            u_data = discord.get('https://discord.com/api/users/@me').json()
            u_id = u_data['id']
            
            # Перевірка на бан при вході
            if cursor.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (u_id,)).fetchone():
                st.error("❌ Ваш доступ заблоковано.")
                st.stop()

            m_data = discord.get(f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member").json()
            u_roles = m_data.get('roles', [])
            is_adm = config['ADMIN_ROLE_ID'] in u_roles
            is_allowed = config['ALLOWED_ROLE_ID'] in u_roles or is_adm
            
            if not is_allowed:
                st.error("❌ Доступ дозволено лише для співробітників.")
                st.stop()

            st.session_state.auth_user = {"id": u_id, "username": u_data['username'], "is_admin": is_adm}
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Помилка авторизації: {e}")

# --- 6. ОСНОВНИЙ ЦИКЛ ---
if not st.session_state.auth_user:
    handle_discord_login()
else:
    user = st.session_state.auth_user
    
    # Перевірка на бан (якщо забанили під час сесії)
    if cursor.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (user['id'],)).fetchone():
        st.error("❌ Ваш аккаунт заблоковано.")
        st.session_state.auth_user = None
        st.stop()

    current_coords = load_user_coords(user['id'])

    # --- САЙДБАР ---
    st.sidebar.title(f"👤 {user['username']}")
    menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

    if menu == "🚪 Вихід":
        st.session_state.auth_user = None
        st.rerun()

    elif menu == "📊 Адмін-панель":
        if not user['is_admin']:
            st.warning("У вас немає прав адміністратора.")
        else:
            st.header("🛡 Панель управління")
            tab_logs, tab_ban = st.tabs(["📝 Історія", "🚫 Бан-система"])
            
            with tab_logs:
                h = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 50").fetchall()
                st.table([{"Юзер": r[1], "Звітів": r[2], "Час": r[3]} for r in h])
            
            with tab_ban:
                col1, col2 = st.columns([1, 2])
                with col1:
                    new_id = st.text_input("Введіть Discord ID")
                    if st.button("Заблокувати"):
                        cursor.execute("INSERT OR IGNORE INTO blacklist VALUES (?)", (new_id,))
                        conn.commit()
                        st.rerun()
                with col2:
                    st.subheader("Список заблокованих")
                    for row in cursor.execute("SELECT user_id FROM blacklist").fetchall():
                        c_id, c_btn = st.columns([3, 1])
                        c_id.code(row[0])
                        if c_btn.button("Розбан", key=f"unban_{row[0]}"):
                            cursor.execute("DELETE FROM blacklist WHERE user_id = ?", (row[0],))
                            conn.commit()
                            st.rerun()

    elif menu == "⚙️ Налаштування":
        st.header("📐 Трафарет")
        if st.button("🗑 Скинути мої зони"):
            save_user_coords(user['id'], {"Surname": None, "Name": None, "ID": None})
            st.rerun()
        f = st.file_uploader("Фото 1080p", type=['png','jpg','jpeg'])
        if f:
            img = Image.open(f).convert("RGB").resize((1920, 1080))
            target = st.selectbox("Що налаштовуємо?", ["Surname", "Name", "ID"])
            rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
            if st.button("💾 Зберегти зону"):
                new_c = current_coords
                new_c[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
                save_user_coords(user['id'], new_c)
                st.success("Збережено!")

    elif menu == "📄 Сканер":
        if not all(current_coords.values()):
            st.warning("⚠️ Налаштуйте трафарет!")
        else:
            st.header("📸 Масовий звіт")
            p_files = st.file_uploader("1. Паспорти", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"p_{st.session_state.file_uploader_key}")
            if p_files and st.button("🔍 Почати OCR"):
                st.session_state.scanned_data = []
                st.session_state.passport_payload = []
                for i, f in enumerate(p_files):
                    img = Image.open(f).convert("RGB").resize((1920, 1080))
                    img_np = np.array(img)
                    res = {}
                    for lbl, (x, y, w, h) in current_coords.items():
                        crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                        txt = " ".join([t[1] for t in reader.readtext(crop)])
                        res[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else re.sub(r'[^a-zA-Z]', '', txt).capitalize()
                    st.session_state.scanned_data.append(res)
                    buf = compress_image(f)
                    st.session_state.passport_payload.append((f"p{i}", (f"p_{i}.jpg", buf.read(), "image/jpeg")))
                st.rerun()

            if st.session_state.scanned_data:
                st.subheader("📝 Корекція даних")
                final = []
                for idx, item in enumerate(st.session_state.scanned_data):
                    c = st.columns([3, 3, 2])
                    s = c[0].text_input(f"Прізвище #{idx+1}", item['Surname'], key=f"s_{idx}")
                    n = c[1].text_input(f"Ім'я #{idx+1}", item['Name'], key=f"n_{idx}")
                    u = c[2].text_input(f"ID #{idx+1}", item['ID'], key=f"u_{idx}")
                    final.append({"Surname": s, "Name": n, "ID": u})
                
                c_files = st.file_uploader("2. Розрахунки", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"c_{st.session_state.file_uploader_key}")
                if st.button("🚀 ВІДПРАВИТИ В DISCORD"):
                    if not c_files: st.error("Завантажте скрини виплат!")
                    else:
                        msg = f"🏥 **НОВИЙ МЕД-ЗВІТ**\nАвтор: <@{user['id']}>\nКількість: {len(final)}\n\n" + \
                              "\n".join([f"• {r['Surname']} {r['Name']} (#{r['ID']})" for r in final])
                        try:
                            # 1. Звіт + Паспорти
                            requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": msg}, files=st.session_state.passport_payload)
                            # 2. Розрахунки
                            c_pay = []
                            for i, cf in enumerate(c_files):
                                c_buf = compress_image(cf)
                                c_pay.append((f"c{i}", (f"c_{i}.jpg", c_buf.read(), "image/jpeg")))
                            requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": "💳 **Докази виплат:**"}, files=c_pay)
                            # 3. Лог
                            cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final), datetime.now().strftime("%Y-%m-%d %H:%M")))
                            conn.commit()
                            st.success("✅ Звіт надіслано!")
                            st.session_state.scanned_data = []
                            st.session_state.file_uploader_key += 1
                            st.rerun()
                        except Exception as e: st.error(f"Помилка: {e}")

