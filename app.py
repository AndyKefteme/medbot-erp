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

# --- 0. НАЛАШТУВАННЯ ДЛЯ STREAMLIT CLOUD ---
# Вказуємо шлях до Tesseract (має бути встановлений через packages.txt)
if os.path.exists('/usr/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

# Дозволяємо роботу OAuth2Session
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# --- 1. КОНФІГУРАЦІЯ (SECRETS) ---
if os.path.exists("config.json"):
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
else:
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
        st.error(f"❌ Помилка: Налаштуйте Secrets у Streamlit Cloud! ({e})")
        st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- 2. БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 3. ФУНКЦІЇ РОЗПІЗНАВАННЯ ---
def ocr_process(image_np, is_id=False):
    # Покращення зображення для Tesseract
    gray = cv2.cvtColor(image_np, cv2.COLOR_RGB2GRAY)
    _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    txt = pytesseract.image_to_string(thresh, config='--psm 7')
    
    if is_id:
        return "".join(re.findall(r'\d+', txt))
    # Лишаємо тільки літери для імен/прізвищ
    return re.sub(r'[^a-zA-Zа-яА-ЯіїєґІЇЄҐ]', '', txt).capitalize()

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
if 'auth_user' not in st.session_state: st.session_state.auth_user = None
if 'scanned_data' not in st.session_state: st.session_state.scanned_data = []
if 'passport_payload' not in st.session_state: st.session_state.passport_payload = []
if 'file_uploader_key' not in st.session_state: st.session_state.file_uploader_key = 0

# --- 5. АВТОРИЗАЦІЯ ---
def handle_discord_login():
    client_id = config['DISCORD_CLIENT_ID']
    redirect_uri = config['DISCORD_REDIRECT_URI']
    scope = "identify guilds guilds.members.read"
    
    # Формування URL
    auth_url = (
        f"https://discord.com/api/oauth2/authorize?"
        f"client_id={client_id}&"
        f"redirect_uri={requests.utils.quote(redirect_uri)}&"
        f"response_type=code&"
        f"scope={requests.utils.quote(scope)}"
    )
    
    st.title("🏥 MedBot ERP System")
    st.write("Будь ласка, авторизуйтесь для доступу до системи.")

    # Метод 1: Нативна кнопка Streamlit + JS перехід
    # Це найбільш стабільний спосіб для Linux-хостингів
    if st.button("🔑 Увійти через Discord", type="primary"):
        js = f"window.top.location.href = '{auth_url}';"
        st.components.v1.html(f"<script>{js}</script>", height=0)
        st.stop()

    # Метод 2: Резервне посилання (якщо кнопка не спрацювала)
    st.markdown(f"""
        <div style="margin-top: 20px;">
            <p style="font-size: 0.8em; color: gray;">Якщо кнопка не спрацювала, 
            <a href="{auth_url}" target="_top">натисніть тут</a></p>
        </div>
    """, unsafe_allow_html=True)
    
    # Обробка повернення з Discord
    qp = st.query_params
    if "code" in qp:
        try:
            discord = OAuth2Session(client_id, redirect_uri=redirect_uri, scope=scope.split())
            token = discord.fetch_token('https://discord.com/api/oauth2/token', 
                                        client_secret=config['DISCORD_CLIENT_SECRET'], 
                                        code=qp['code'])
            u_data = discord.get('https://discord.com/api/users/@me').json()
            
            # Перевірка ролей
            m_data = discord.get(f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member").json()
            u_roles = m_data.get('roles', [])
            is_adm = config['ADMIN_ROLE_ID'] in u_roles
            is_allowed = config['ALLOWED_ROLE_ID'] in u_roles or is_adm
            
            if is_allowed:
                st.session_state.auth_user = {"id": u_data['id'], "username": u_data['username'], "is_admin": is_adm}
                st.query_params.clear()
                st.rerun()
            else:
                st.error("❌ У вас немає доступу (відсутня роль).")
        except Exception as e:
            st.error(f"Помилка авторизації: {e}")

# Перевірка входу
if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

user = st.session_state.auth_user

# Перевірка чорного списку
if cursor.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (user['id'],)).fetchone():
    st.error("❌ Ваш доступ заблоковано адміністратором.")
    st.stop()

# --- 6. ОСНОВНИЙ ІНТЕРФЕЙС (ВАШ ДИЗАЙН) ---
st.sidebar.title(f"👤 {user['username']}")
if user['is_admin']: st.sidebar.subheader("👑 Адміністратор")
else: st.sidebar.caption("🩺 Співробітник")

menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.auth_user = None
    st.rerun()

elif menu == "📊 Адмін-панель" and user['is_admin']:
    st.header("🛡 Управління системою")
    t_logs, t_ban = st.tabs(["📝 Останні звіти", "🚫 Блокування"])
    with t_logs:
        h = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 50").fetchall()
        st.table([{"Користувач": r[1], "К-сть паспортів": r[2], "Дата": r[3]} for r in h])
    with t_ban:
        bid = st.text_input("Введіть Discord ID користувача")
        if st.button("Заблокувати"):
            cursor.execute("INSERT OR IGNORE INTO blacklist VALUES (?)", (bid,))
            conn.commit()
            st.success(f"Користувача {bid} додано до чорного списку!")

elif menu == "⚙️ Налаштування":
    st.header("📐 Налаштування трафарету")
    if st.button("🗑 Скинути всі зони"):
        save_user_coords(user['id'], {"Surname": None, "Name": None, "ID": None})
        st.rerun()
        
    f = st.file_uploader("Завантажте фото-зразок для налаштування", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Виберіть поле для налаштування", ["Surname", "Name", "ID"])
        st.info(f"Виділіть зону для: {target}")
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        
        if st.button("💾 Зберегти зону"):
            coords = load_user_coords(user['id'])
            coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_user_coords(user['id'], coords)
            st.success(f"Зону розпізнавання для '{target}' збережено!")

elif menu == "📄 Сканер":
    current_coords = load_user_coords(user['id'])
    if not all(current_coords.values()):
        st.warning("⚠️ Спочатку налаштуйте зони розпізнавання в меню 'Налаштування'!")
    else:
        st.header("📸 Масове розпізнавання паспортів")
        p_files = st.file_uploader("1. Завантажте фото паспортів", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"p_{st.session_state.file_uploader_key}")
        
        if p_files and st.button("🔍 Почати сканування"):
            st.session_state.scanned_data = []
            st.session_state.passport_payload = []
            
            with st.spinner("Обробка зображень..."):
                for i, f in enumerate(p_files):
                    img_pil = Image.open(f).convert("RGB").resize((1920, 1080))
                    img_np = np.array(img_pil)
                    res = {}
                    for lbl, (x, y, w, h) in current_coords.items():
                        crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                        res[lbl] = ocr_process(crop, is_id=(lbl=="ID"))
                    
                    st.session_state.scanned_data.append(res)
                    buf = compress_image(f)
                    st.session_state.passport_payload.append((f"p{i}", (f"p_{i}.jpg", buf.read(), "image/jpeg")))
            st.rerun()

        if st.session_state.scanned_data:
            st.subheader("📝 Перевірка та редагування")
            final_results = []
            for idx, item in enumerate(st.session_state.scanned_data):
                cols = st.columns([3, 3, 2])
                s = cols[0].text_input(f"Прізвище #{idx+1}", item['Surname'], key=f"s_{idx}")
                n = cols[1].text_input(f"Ім'я #{idx+1}", item['Name'], key=f"n_{idx}")
                u = cols[2].text_input(f"ID #{idx+1}", item['ID'], key=f"u_{idx}")
                final_results.append({"Surname": s, "Name": n, "ID": u})
            
            c_files = st.file_uploader("2. Додайте фото чеків/виплат", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"c_{st.session_state.file_uploader_key}")
            
            if st.button("🚀 ВІДПРАВИТИ ЗВІТ У DISCORD"):
                if not c_files:
                    st.error("Помилка: Необхідно додати фото доказів виплат!")
                else:
                    with st.spinner("Надсилання даних..."):
                        report_text = f"🏥 **НОВИЙ МЕД-ЗВІТ**\nАвтор: <@{user['id']}> ({user['username']})\n\n" + \
                                     "\n".join([f"• {r['Surname']} {r['Name']} (ID: {r['ID']})" for r in final_results])
                        
                        try:
                            # 1. Надсилаємо текст і паспорти
                            requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": report_text}, files=st.session_state.passport_payload)
                            
                            # 2. Надсилаємо чеки
                            checks_payload = []
                            for i, cf in enumerate(c_files):
                                c_buf = compress_image(cf)
                                checks_payload.append((f"c{i}", (f"c_{i}.jpg", c_buf.read(), "image/jpeg")))
                            requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": "💳 **Фото доказів виплат:**"}, files=checks_payload)
                            
                            # 3. Логування
                            cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final_results), datetime.now().strftime("%d.%m.%Y %H:%M")))
                            conn.commit()
                            
                            st.success("✅ Звіт успішно надіслано в Discord!")
                            st.session_state.scanned_data = []
                            st.session_state.file_uploader_key += 1 # Скидання завантажувачів
                            st.rerun()
                        except Exception as e:
                            st.error(f"❌ Помилка при надсиланні: {e}")

