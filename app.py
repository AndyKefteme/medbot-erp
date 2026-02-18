import streamlit as st
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

# --- 0. НАЛАШТУВАННЯ TESSERACT ДЛЯ LINUX (STREAMLIT CLOUD) ---
# На Streamlit Cloud шлях зазвичай саме такий. 
# Це виправить "зависання" при спробі викликати OCR.
if os.path.exists('/usr/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

# --- 1. УНІВЕРСАЛЬНА КОНФІГУРАЦІЯ ---
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

if os.path.exists("config.json"):
    # Локальний запуск
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
else:
    # Запуск на хостингу (Streamlit Cloud)
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
        st.error(f"❌ Помилка конфігурації: Перевірте 'Secrets' у налаштуваннях Streamlit. ({e})")
        st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP", page_icon="🏥")

# --- 2. БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 3. ФУНКЦІЇ РОЗПІЗНАВАННЯ (Tesseract) ---
def ocr_process(image_np):
    # Конвертація для кращого розпізнавання
    gray = cv2.cvtColor(image_np, cv2.COLOR_RGB2GRAY)
    _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    text = pytesseract.image_to_string(thresh, config='--psm 7')
    return text.strip()

def save_user_coords(u_id, coords):
    cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (u_id, json.dumps(coords)))
    conn.commit()

def load_user_coords(u_id):
    saved = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    return json.loads(saved[0]) if saved else {"Surname": None, "Name": None, "ID": None}

def compress_image(image_file):
    img = Image.open(image_file).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    buf.seek(0)
    return buf

# --- 4. СТАН СЕСІЇ ---
if 'auth_user' not in st.session_state: st.session_state.auth_user = None
if 'scanned_data' not in st.session_state: st.session_state.scanned_data = []
if 'passport_payload' not in st.session_state: st.session_state.passport_payload = []
if 'file_uploader_key' not in st.session_state: st.session_state.file_uploader_key = 0

# --- 5. АВТОРИЗАЦІЯ ---
def handle_discord_login():
    client_id = config['DISCORD_CLIENT_ID']
    redirect_uri = config['DISCORD_REDIRECT_URI']
    scope = "identify guilds guilds.members.read"
    auth_url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&scope={scope}"
    
    st.title("🏥 MedBot ERP System")
    st.info("Будь ласка, авторизуйтесь через Discord.")
    
    # Використовуємо надійну HTML кнопку для переходу
    st.markdown(f'''
        <a href="{auth_url}" target="_blank">
            <button style="background-color: #5865F2; color: white; border: none; padding: 15px 30px; border-radius: 8px; font-weight: bold; font-size: 18px; cursor: pointer; width: 100%;">
                🔑 Увійти через Discord
            </button>
        </a>
    ''', unsafe_allow_html=True)
    
    qp = st.query_params
    if "code" in qp:
        try:
            discord = OAuth2Session(client_id, redirect_uri=redirect_uri, scope=scope.split())
            token = discord.fetch_token('https://discord.com/api/oauth2/token', client_secret=config['DISCORD_CLIENT_SECRET'], code=qp['code'])
            u_data = discord.get('https://discord.com/api/users/@me').json()
            
            # Перевірка ролей
            m_data = discord.get(f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member").json()
            u_roles = m_data.get('roles', [])
            is_adm = config['ADMIN_ROLE_ID'] in u_roles
            
            if config['ALLOWED_ROLE_ID'] in u_roles or is_adm:
                st.session_state.auth_user = {"id": u_data['id'], "username": u_data['username'], "is_admin": is_adm}
                st.query_params.clear()
                st.rerun()
            else:
                st.error("❌ У вас немає потрібної ролі в Discord.")
        except Exception as e:
            st.error(f"Помилка входу: {e}")

# --- 6. ГОЛОВНИЙ МОДУЛЬ ---
if not st.session_state.auth_user:
    handle_discord_login()
else:
    user = st.session_state.auth_user
    # Перевірка бана
    if cursor.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (user['id'],)).fetchone():
        st.error("❌ Ваш доступ анульовано.")
        st.stop()

    current_coords = load_user_coords(user['id'])

    st.sidebar.title(f"👤 {user['username']}")
    menu = st.sidebar.radio("Меню", ["📄 Сканер", "⚙️ Трафарет", "📊 Адмінка", "🚪 Вихід"])

    if menu == "🚪 Вихід":
        st.session_state.auth_user = None
        st.rerun()

    elif menu == "⚙️ Трафарет":
        st.header("📐 Налаштування зон")
        f = st.file_uploader("Завантажте зразок (1080p)", type=['jpg','jpeg','png'])
        if f:
            img = Image.open(f).convert("RGB").resize((1920, 1080))
            target = st.selectbox("Виберіть поле", ["Surname", "Name", "ID"])
            rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
            if st.button("💾 Зберегти"):
                current_coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
                save_user_coords(user['id'], current_coords)
                st.success(f"Зону {target} збережено!")

    elif menu == "📄 Сканер":
        if not all(current_coords.values()):
            st.warning("⚠️ Спочатку налаштуйте зони в меню 'Трафарет'!")
        else:
            st.header("📸 Масове розпізнавання")
            p_files = st.file_uploader("1. Фото паспортів", accept_multiple_files=True, type=['jpg','png'], key=f"p_{st.session_state.file_uploader_key}")
            
            if p_files and st.button("🔍 Почати сканування"):
                st.session_state.scanned_data = []
                st.session_state.passport_payload = []
                progress = st.progress(0)
                
                for i, f in enumerate(p_files):
                    img = Image.open(f).convert("RGB").resize((1920, 1080))
                    img_np = np.array(img)
                    res = {}
                    for lbl, (x, y, w, h) in current_coords.items():
                        crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                        res[lbl] = ocr_process(crop)
                    
                    st.session_state.scanned_data.append(res)
                    buf = compress_image(f)
                    st.session_state.passport_payload.append((f"p{i}", (f"p_{i}.jpg", buf.read(), "image/jpeg")))
                    progress.progress((i + 1) / len(p_files))
                st.rerun()

            if st.session_state.scanned_data:
                st.subheader("📝 Перевірка даних")
                final_results = []
                for idx, item in enumerate(st.session_state.scanned_data):
                    cols = st.columns([3, 3, 2])
                    s = cols[0].text_input(f"Прізвище #{idx+1}", item['Surname'], key=f"s_{idx}")
                    n = cols[1].text_input(f"Ім'я #{idx+1}", item['Name'], key=f"n_{idx}")
                    u = cols[2].text_input(f"ID #{idx+1}", item['ID'], key=f"u_{idx}")
                    final_results.append({"Surname": s, "Name": n, "ID": u})
                
                c_files = st.file_uploader("2. Фото виплат (чеки)", accept_multiple_files=True, type=['jpg','png'], key=f"c_{st.session_state.file_uploader_key}")
                
                if st.button("🚀 ВІДПРАВИТИ ЗВІТ"):
                    if not c_files:
                        st.error("Додайте фото розрахунків!")
                    else:
                        with st.spinner("Надсилання..."):
                            msg = f"🏥 **НОВИЙ МЕД-ЗВІТ**\nВід: <@{user['id']}>\n\n" + \
                                  "\n".join([f"• {r['Surname']} {r['Name']} (ID: {r['ID']})" for r in final_results])
                            
                            # Відправка паспортів
                            requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": msg}, files=st.session_state.passport_payload)
                            
                            # Відправка чеків
                            check_payload = []
                            for i, cf in enumerate(c_files):
                                c_buf = compress_image(cf)
                                check_payload.append((f"c{i}", (f"c_{i}.jpg", c_buf.read(), "image/jpeg")))
                            requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": "💳 **Фото виплат:**"}, files=check_payload)
                            
                            cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final_results), datetime.now().strftime("%H:%M %d.%m.%Y")))
                            conn.commit()
                            
                            st.success("✅ Звіт успішно надіслано в Discord!")
                            st.session_state.scanned_data = []
                            st.session_state.file_uploader_key += 1
                            st.rerun()

    elif menu == "📊 Адмінка" and user['is_admin']:
        st.header("🛡 Управління")
        rows = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC").fetchall()
        st.table([{"Користувач": r[1], "Звітів": r[2], "Дата": r[3]} for r in rows])


