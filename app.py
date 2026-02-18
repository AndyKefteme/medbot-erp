import streamlit as st
import streamlit.components.v1 as components  # ДОДАНО ЦЕЙ ІМПОРТ
import cv2
import numpy as np
import pytesseract
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

# --- 0. АДАПТАЦІЯ ПІД LINUX/STREAMLIT ---
if os.path.exists('/usr/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# --- 1. КОНФІГУРАЦІЯ (SECRETS) ---
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
    st.error(f"Помилка конфігурації Secrets: {e}")
    st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- 2. БАЗА ДАНИХ (Використовуємо абсолютний шлях для стабільності) ---
DB_PATH = os.path.join(os.getcwd(), "medbot_db.sqlite")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 3. OCR ФУНКЦІЇ ---
def ocr_process(image_np, is_id=False):
    try:
        gray = cv2.cvtColor(image_np, cv2.COLOR_RGB2GRAY)
        _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        txt = pytesseract.image_to_string(thresh, config='--psm 7')
        if is_id: return "".join(re.findall(r'\d+', txt))
        return re.sub(r'[^a-zA-Zа-яА-ЯіїєґІЇЄҐ]', '', txt).capitalize()
    except Exception as e:
        return f"Помилка OCR: {e}"

def load_user_coords(u_id):
    cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,))
    saved = cursor.fetchone()
    return json.loads(saved[0]) if saved else {"Surname": None, "Name": None, "ID": None}

def save_user_coords(u_id, coords):
    cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (u_id, json.dumps(coords)))
    conn.commit()

# --- 4. АВТОРИЗАЦІЯ (ВИПРАВЛЕНО) ---
if 'auth_user' not in st.session_state:
    st.session_state.auth_user = None

def handle_discord_login():
    client_id = config['DISCORD_CLIENT_ID']
    redirect_uri = config['DISCORD_REDIRECT_URI']
    scope = "identify guilds guilds.members.read"
    
    # Правильне формування URL
    encoded_uri = requests.utils.quote(redirect_uri)
    encoded_scope = requests.utils.quote(scope)
    auth_url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&redirect_uri={encoded_uri}&response_type=code&scope={encoded_scope}"
    
    st.title("🏥 MedBot ERP System")
    st.info("Натисніть кнопку нижче для авторизації через Discord.")

    # ВИКОРИСТОВУЄМО components ТУТ
    if st.button("🔑 Увійти через Discord", type="primary"):
        components.html(f"<script>window.top.location.href = '{auth_url}';</script>", height=0)
        st.stop()

    # Перевірка коду в URL
    if "code" in st.query_params:
        code = st.query_params["code"]
        try:
            discord = OAuth2Session(client_id, redirect_uri=redirect_uri, scope=scope.split())
            token = discord.fetch_token('https://discord.com/api/oauth2/token', 
                                        client_secret=config['DISCORD_CLIENT_SECRET'], 
                                        code=code)
            u_data = discord.get('https://discord.com/api/users/@me').json()
            
            # Перевірка гільдії
            guild_url = f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member"
            m_data = discord.get(guild_url).json()
            
            u_roles = m_data.get('roles', [])
            is_adm = config['ADMIN_ROLE_ID'] in u_roles
            is_allowed = config['ALLOWED_ROLE_ID'] in u_roles or is_adm
            
            if is_allowed:
                st.session_state.auth_user = {"id": u_data['id'], "username": u_data['username'], "is_admin": is_adm}
                # Очищення параметрів після входу
                st.query_params.clear()
                st.rerun()
            else:
                st.error("❌ Доступ заборонено: відсутня потрібна роль у Discord.")
        except Exception as e:
            st.error(f"Помилка авторизації: {e}")

if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

# --- 5. ОСНОВНИЙ ІНТЕРФЕЙС (ВАШ ВІЗУАЛ) ---
user = st.session_state.auth_user
st.sidebar.title(f"👤 {user['username']}")
if user['is_admin']: st.sidebar.subheader("👑 Адміністратор")

menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмінка", "🚪 Вихід"])

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
        if st.button("💾 Зберегти зону"):
            coords = load_user_coords(user['id'])
            coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_user_coords(user['id'], coords)
            st.success(f"Зону {target} збережено!")

elif menu == "📄 Сканер":
    coords = load_user_coords(user['id'])
    if not all(coords.values()):
        st.warning("⚠️ Спочатку налаштуйте трафарет у Налаштуваннях!")
    else:
        st.header("📸 Сканер паспортів")
        p_files = st.file_uploader("Завантажте паспорти", accept_multiple_files=True, type=['png','jpg','jpeg'])
        if p_files and st.button("🔍 Почати розпізнавання"):
            results = []
            for f in p_files:
                img = Image.open(f).convert("RGB").resize((1920, 1080))
                img_np = np.array(img)
                res = {}
                for lbl, (x, y, w, h) in coords.items():
                    crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                    res[lbl] = ocr_process(crop, is_id=(lbl=="ID"))
                results.append(res)
            st.session_state.scanned_data = results
            st.rerun()

        if st.session_state.get('scanned_data'):
            st.subheader("📝 Перевірка даних")
            for idx, item in enumerate(st.session_state.scanned_data):
                cols = st.columns(3)
                item['Surname'] = cols[0].text_input(f"Прізвище #{idx}", item['Surname'], key=f"sur_{idx}")
                item['Name'] = cols[1].text_input(f"Ім'я #{idx}", item['Name'], key=f"nam_{idx}")
                item['ID'] = cols[2].text_input(f"ID #{idx}", item['ID'], key=f"id_{idx}")
            
            if st.button("🚀 Відправити звіт"):
                msg = f"🏥 **Звіт від <@{user['id']}>**\n" + "\n".join([f"• {r['Surname']} {r['Name']} (ID: {r['ID']})" for r in st.session_state.scanned_data])
                requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": msg})
                
                # Логування в БД
                cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(st.session_state.scanned_data), datetime.now().strftime("%d.%m.%Y %H:%M")))
                conn.commit()
                
                st.success("✅ Звіт надіслано!")
                st.session_state.scanned_data = []

elif menu == "📊 Адмінка" and user['is_admin']:
    st.header("📊 Статистика звітів")
    logs = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC").fetchall()
    st.table([{"Користувач": r[1], "К-сть": r[2], "Дата": r[3]} for r in logs])
