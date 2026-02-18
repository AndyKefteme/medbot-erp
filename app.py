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

# --- НАЛАШТУВАННЯ СЕРВЕРНОЇ ЧАСТИНИ ДЛЯ DISCLOUD ---
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

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
if 'scanned_data' not in st.session_state:
    st.session_state.scanned_data = []
if 'passport_payload' not in st.session_state:
    st.session_state.passport_payload = []
if 'file_uploader_key' not in st.session_state:
    st.session_state.file_uploader_key = 0

# --- АВТОРИЗАЦІЯ DISCORD (АВТО-РЕДИРЕКТ) ---
def handle_discord_login():
    # Посилання для автоматичного переходу (використовуємо твій лінк)
    auth_url = "https://discord.com/api/oauth2/authorize?response_type=code&client_id=1473419565978615929&redirect_uri=https%3A%2F%2Fems-zvit.streamlit.app&scope=identify+guilds+guilds.members.read&state=edKLeYvUD7lV7nbkhdvRKfNAxcWKpZ"
    
    qp = st.query_params
    
    # Якщо коду немає в URL — значить юзер тільки зайшов, кидаємо його в Discord
    if "code" not in qp:
        st.write("Redirecting to Discord...")
        # Використовуємо JS для миттєвого переходу
        st.components.v1.html(f"""
            <script>
                window.parent.location.href = "{auth_url}";
            </script>
        """, height=0)
        st.stop()
    
    # Якщо код є — обробляємо його
    if "code" in qp:
        try:
            # Створюємо сесію для обміну токена
            discord = OAuth2Session(config['DISCORD_CLIENT_ID'], redirect_uri=config['DISCORD_REDIRECT_URI'])
            token = discord.fetch_token('https://discord.com/api/oauth2/token', 
                                        client_secret=config['DISCORD_CLIENT_SECRET'], 
                                        code=qp['code'])
            
            u_data = discord.get('https://discord.com/api/users/@me').json()
            u_id = u_data['id']
            
            # Перевірка бану
            if cursor.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (u_id,)).fetchone():
                st.error("❌ Ваш доступ заблоковано.")
                st.stop()

            # Перевірка ролей
            m_url = f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member"
            m_data = discord.get(m_url).json()
            u_roles = m_data.get('roles', [])
            is_adm = config['ADMIN_ROLE_ID'] in u_roles
            is_allowed = config['ALLOWED_ROLE_ID'] in u_roles or is_adm
            
            if not is_allowed:
                st.error("❌ У вас немає доступу.")
                st.stop()

            # Зберігаємо юзера і чистимо URL від кодів
            st.session_state.auth_user = {"id": u_id, "username": u_data['username'], "is_admin": is_adm}
            st.query_params.clear()
            st.rerun()
            
        except Exception as e:
            st.error(f"Помилка входу: {e}")
            if st.button("Спробувати ще раз"):
                st.query_params.clear()
                st.rerun()

# Перевірка авторизації
if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

# --- РЕШТА ТВОГО КОДУ (БЕЗ ЗМІН) ---
user = st.session_state.auth_user
current_coords = load_user_coords(user['id'])

st.sidebar.title(f"👤 {user['username']}")
if user['is_admin']: st.sidebar.subheader("👑 Адміністратор")
else: st.sidebar.caption("🩺 Співробітник")

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
            c1, c2 = st.columns([1, 2])
            with c1:
                bid = st.text_input("Discord ID")
                if st.button("🚫 Бан"):
                    cursor.execute("INSERT OR IGNORE INTO blacklist VALUES (?)", (bid,))
                    conn.commit()
                    st.rerun()
            with c2:
                st.subheader("Список")
                for r in cursor.execute("SELECT user_id FROM blacklist").fetchall():
                    bc1, bc2 = st.columns([3, 1])
                    bc1.code(r[0])
                    if bc2.button("🗑", key=f"u_{r[0]}"):
                        cursor.execute("DELETE FROM blacklist WHERE user_id = ?", (r[0],))
                        conn.commit()
                        st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Трафарет")
    if st.button("🗑 Очистити координати"):
        save_user_coords(user['id'], {"Surname": None, "Name": None, "ID": None})
        st.rerun()
    f = st.file_uploader("Завантажте зразок", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Зона", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        if st.button("💾 Зберегти"):
            new_c = current_coords
            new_c[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_user_coords(user['id'], new_c)
            st.rerun()

elif menu == "📄 Сканер":
    if not all(current_coords.values()):
        st.warning("⚠️ Налаштуйте координати!")
    else:
        st.header("📸 Новий звіт")
        p_files = st.file_uploader("1. Паспорти (макс. 10)", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"p_{st.session_state.file_uploader_key}")
        if p_files and st.button("🔍 Розпізнати"):
            st.session_state.scanned_data = []
            st.session_state.passport_payload = []
            for i, f in enumerate(p_files):
                img_pil = Image.open(f).convert("RGB").resize((1920, 1080))
                img_np = np.array(img_pil)
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
                    msg = f"🏥 **ЗВІТ**\n<@{user['id']}> | {user['username']}\nК-сть: {len(final)}\n\n" + \
                          "\n".join([f"{r['Surname']} {r['Name']} #{r['ID']}" for r in final])
                    try:
                        requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": msg}, files=st.session_state.passport_payload)
                        c_pay = []
                        for i, cf in enumerate(c_files):
                            c_buf = compress_image(cf)
                            c_pay.append((f"c{i}", (f"c_{i}.jpg", c_buf.read(), "image/jpeg")))
                        requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": "💳 **Докази:**"}, files=c_pay)
                        cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final), datetime.now().strftime("%Y-%m-%d %H:%M")))
                        conn.commit()
                        st.success("✅ Надіслано!")
                        st.session_state.scanned_data = []
                        st.session_state.file_uploader_key += 1
                        st.rerun()
                    except Exception as e: st.error(f"Помилка: {e}")
