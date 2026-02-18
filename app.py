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

# --- 1. СИСТЕМНІ НАЛАШТУВАННЯ ---
# Це дозволяє OAuth працювати через HTTPS проксі Streamlit
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Папка для моделей OCR (щоб не було NameError)
MODEL_DIR = os.path.join(os.getcwd(), "ocr_models")
if not os.path.exists(MODEL_DIR):
    os.makedirs(MODEL_DIR)

# --- 2. ПЕРЕВІРКА SECRETS ---
if "discord" not in st.secrets:
    st.error("Помилка: Налаштуйте секцію [discord] у Secrets на Streamlit Cloud!")
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

# --- 4. ЗАВАНТАЖЕННЯ OCR (З КЕШУВАННЯМ) ---
@st.cache_resource(show_spinner=False)
def load_reader():
    with st.spinner("🏥 Завантаження модулів розпізнавання... Зачекайте."):
        return easyocr.Reader(['en', 'uk'], gpu=False, model_storage_directory=MODEL_DIR)

# --- 5. ЛОГІКА АВТОРИЗАЦІЇ (МАКСИМАЛЬНО СУМІСНА) ---
def login_page():
    # ПЕРЕВІРКА: Адреса має бути https://ems-zvit.streamlit.app
    redirect_uri = config['DISCORD_REDIRECT_URI'].strip()
    client_id = config['DISCORD_CLIENT_ID'].strip()
    
    # Створюємо пряме посилання для авторизації (без зайвих посередників)
    scope = "identify guilds guilds.members.read"
    auth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={requests.utils.quote(redirect_uri)}"
        f"&response_type=code"
        f"&scope={requests.utils.quote(scope)}"
    )

    st.title("🏥 MedBot ERP System")
    st.markdown("---")
    
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.info("👋 Вітаємо! Потрібна авторизація.")
        
        # ВИКОРИСТОВУЄМО HTML КНОПКУ ЯКА ВІДКРИВАЄ ПОСИЛАННЯ В ТОМУ Ж ВІКНІ
        st.markdown(f'''
            <div style="text-align: center; margin-top: 20px;">
                <a href="{auth_url}" target="_self" style="
                    background-color: #5865F2; 
                    color: white; 
                    padding: 16px 42px; 
                    text-decoration: none; 
                    border-radius: 12px; 
                    font-weight: bold; 
                    font-size: 22px;
                    display: inline-block;
                    box-shadow: 0 6px 20px rgba(88,101,242,0.4);
                ">🔑 УВІЙТИ ЧЕРЕЗ DISCORD</a>
            </div>
        ''', unsafe_allow_html=True)
        
        st.markdown("<p style='text-align:center; color:gray; margin-top:10px;'>Натисніть кнопку вище, щоб перейти до Discord</p>", unsafe_allow_html=True)

    # Обробка повернення з кодом
    params = st.query_params
    if "code" in params:
        try:
            discord = OAuth2Session(client_id, redirect_uri=redirect_uri, scope=scope.split())
            token = discord.fetch_token(
                'https://discord.com/api/oauth2/token',
                client_secret=config['DISCORD_CLIENT_SECRET'].strip(),
                code=params["code"]
            )
            user_data = discord.get('https://discord.com/api/users/@me').json()
            u_id = user_data['id']

            # Перевірка ролі на сервері
            member_url = f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member"
            member_resp = discord.get(member_url)
            
            if member_resp.status_code != 200:
                st.error("❌ Не вдалося отримати дані про ваші ролі на сервері.")
                return

            member_data = member_resp.json()
            roles = member_data.get('roles', [])
            
            is_admin = config['ADMIN_ROLE_ID'] in roles
            is_allowed = config['ALLOWED_ROLE_ID'] in roles or is_admin
            
            if not is_allowed:
                st.error("🚫 Доступ закритий: у вас немає потрібної ролі.")
                return

            st.session_state.auth_user = {"id": u_id, "username": user_data['username'], "is_admin": is_admin}
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Помилка під час входу: {e}")

# Перевірка: чи авторизований користувач
if 'auth_user' not in st.session_state:
    login_page()
    st.stop()

# --- 6. РОБОЧИЙ ІНТЕРФЕЙС ---
reader = load_reader()
user = st.session_state.auth_user

def get_coords(u_id):
    res = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    return json.loads(res[0]) if res else {"Surname": None, "Name": None, "ID": None}

def save_coords(u_id, data):
    cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (u_id, json.dumps(data)))
    conn.commit()

current_c = get_coords(user['id'])

st.sidebar.title(f"👤 {user['username']}")
page = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if page == "🚪 Вихід":
    st.session_state.auth_user = None
    st.rerun()

elif page == "⚙️ Налаштування":
    st.header("📐 Калібрування сканера")
    f = st.file_uploader("Завантажте зразок", type=['jpg', 'png', 'jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Що налаштовуємо?", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='blue', return_type='box')
        if st.button("💾 Зберегти координати"):
            current_c[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_coords(user['id'], current_c)
            st.success("Налаштування збережено!")

elif page == "📄 Сканер":
    if not all(current_c.values()):
        st.warning("⚠️ Спочатку налаштуйте зони у вкладці 'Налаштування'!")
    else:
        st.header("📸 Сканування паспортів")
        files = st.file_uploader("Завантажте фото", accept_multiple_files=True, type=['jpg', 'png', 'jpeg'])
        if files and st.button("🔍 Розпізнати текст"):
            results = []
            p_bar = st.progress(0)
            for i, f in enumerate(files):
                img_np = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                item = {}
                for lbl, (x, y, w, h) in current_c.items():
                    crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                    txt = " ".join([t[1] for t in reader.readtext(crop)])
                    item[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else txt.strip().capitalize()
                results.append(item)
                p_bar.progress((i + 1) / len(files))
            st.session_state.temp_res = results
            st.rerun()

        if 'temp_res' in st.session_state:
            st.subheader("📝 Перевірка")
            final_data = []
            for i, res in enumerate(st.session_state.temp_res):
                c1, c2, c3 = st.columns(3)
                s = c1.text_input(f"Прізвище #{i+1}", res['Surname'], key=f"s{i}")
                n = c2.text_input(f"Ім'я #{i+1}", res['Name'], key=f"n{i}")
                u = c3.text_input(f"ID #{i+1}", res['ID'], key=f"u{i}")
                final_data.append({"Surname": s, "Name": n, "ID": u})
            
            if st.button("🚀 ВІДПРАВИТИ ЗВІТ"):
                msg = f"🏥 **Новий звіт** від <@{user['id']}>\n" + "\n".join([f"• {x['Surname']} {x['Name']} (ID: {x['ID']})" for x in final_data])
                requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": msg})
                cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final_data), datetime.now().strftime("%Y-%m-%d %H:%M")))
                conn.commit()
                st.success("✅ Надіслано в Discord!")
                del st.session_state.temp_res

elif page == "📊 Адмін-панель":
    if user['is_admin']:
        st.header("📊 Останні звіти")
        data = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 50").fetchall()
        st.table([{"Юзер": r[1], "Кількість": r[2], "Дата": r[3]} for r in data])
