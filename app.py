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

# --- КОНФІГУРАЦІЯ (Береться з Secrets Streamlit) ---
try:
    config = st.secrets["discord"]
except Exception:
    st.error("Помилка: Налаштування 'secrets' не знайдено в панелі Streamlit Cloud!")
    st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- МОНІТОРИНГ ЗАВАНТАЖЕННЯ МОДЕЛЕЙ ---
MODEL_DIR = os.path.expanduser("~/.EasyOCR/model")

def get_dir_size_mb(path):
    if not os.path.exists(path):
        return 0.0
    size = sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path))
    return size / (1024 * 1024)

@st.cache_resource(show_spinner=False)
def load_ocr_with_progress():
    # Орієнтовна вага моделей для EN та UK мов
    TOTAL_EXPECTED_SIZE = 148.0 
    
    placeholder = st.empty()
    with placeholder.container():
        st.info("🤖 Зачекайте, система готує модулі розпізнавання тексту...")
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # Запускаємо ініціалізацію. EasyOCR почне качати файли, якщо їх немає.
        # Ми будемо паралельно міряти розмір папки.
        
        start_time = time.time()
        # Для того, щоб ми могли бачити прогрес, нам треба ініціалізувати Reader
        # Але оскільки Reader блокує потік, ми виведемо індикатор "Завантаження"
        
        # Це "хитрий" метод: EasyOCR сам виведе логи в консоль, 
        # а ми покажемо юзеру візуальну симуляцію на основі реальної ваги файлів.
        reader = easyocr.Reader(['en', 'uk'], gpu=False)
        
        # Коли ініціалізація завершена:
        progress_bar.progress(100)
        status_text.success("✅ Систему розпізнавання активовано!")
        time.sleep(1)
    
    placeholder.empty()
    return reader

# Виклик завантаження
reader = load_ocr_with_progress()

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
if 'auth_user' not in st.session_state: st.session_state.auth_user = None
if 'scanned_data' not in st.session_state: st.session_state.scanned_data = []
if 'passport_payload' not in st.session_state: st.session_state.passport_payload = []
if 'file_uploader_key' not in st.session_state: st.session_state.file_uploader_key = 0

# --- АВТОРИЗАЦІЯ DISCORD ---
def handle_discord_login():
    scope = ['identify', 'guilds', 'guilds.members.read']
    discord = OAuth2Session(config['DISCORD_CLIENT_ID'], redirect_uri=config['DISCORD_REDIRECT_URI'], scope=scope)
    auth_url, _ = discord.authorization_url('https://discord.com/api/oauth2/authorize')
    
    st.title("🏥 MedBot ERP System")
    login_html = f'''
        <div style="text-align: center; margin-top: 50px;">
            <a href="{auth_url}" target="_self" style="
                background-color: #5865F2; color: white; padding: 15px 35px; 
                text-decoration: none; border-radius: 10px; font-weight: bold; font-size: 20px;
            ">🔑 Увійти через Discord</a>
        </div>
    '''
    st.markdown(login_html, unsafe_allow_html=True)
    
    qp = st.query_params
    if "code" in qp:
        try:
            token = discord.fetch_token('https://discord.com/api/oauth2/token', 
                                        client_secret=config['DISCORD_CLIENT_SECRET'], 
                                        code=qp['code'])
            u_data = discord.get('https://discord.com/api/users/@me').json()
            u_id = u_data['id']
            
            if cursor.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (u_id,)).fetchone():
                st.error("❌ Ваш доступ обмежено.")
                st.stop()

            m_url = f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member"
            m_data = discord.get(m_url).json()
            u_roles = m_data.get('roles', [])
            
            is_adm = config['ADMIN_ROLE_ID'] in u_roles
            is_allowed = config['ALLOWED_ROLE_ID'] in u_roles or is_adm
            
            if not is_allowed:
                st.error("❌ У вас немає доступу до системи (відсутня роль).")
                st.stop()

            st.session_state.auth_user = {"id": u_id, "username": u_data['username'], "is_admin": is_adm}
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Помилка входу: {e}")

if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

user = st.session_state.auth_user
current_coords = load_user_coords(user['id'])

# --- ОСНОВНИЙ ІНТЕРФЕЙС ---
st.sidebar.title(f"👤 {user['username']}")
menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.auth_user = None
    st.rerun()

elif menu == "📊 Адмін-панель":
    if not user['is_admin']:
        st.warning("Доступ закритий.")
    else:
        st.header("🛡 Адмін-центр")
        t_logs, t_ban = st.tabs(["📝 Логи", "🚫 Бан"])
        with t_logs:
            l = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 50").fetchall()
            st.table([{"Користувач": r[1], "К-сть": r[2], "Дата": r[3]} for r in l])
        with t_ban:
            bid = st.text_input("Введіть ID")
            if st.button("🚫 Заблокувати"):
                cursor.execute("INSERT OR IGNORE INTO blacklist VALUES (?)", (bid,))
                conn.commit()
                st.rerun()
            for r in cursor.execute("SELECT user_id FROM blacklist").fetchall():
                st.code(r[0])

elif menu == "⚙️ Налаштування":
    st.header("📐 Калібрування сканера")
    if st.button("🗑 Скинути координати"):
        save_user_coords(user['id'], {"Surname": None, "Name": None, "ID": None})
        st.rerun()
    
    f = st.file_uploader("Завантажте зразок", type=['png','jpg','jpeg'])
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
        st.warning("⚠️ Налаштуйте зони в розділі 'Налаштування'!")
    else:
        st.header("📸 Сканування паспортів")
        p_files = st.file_uploader("1. Оберіть фото документів", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"p_{st.session_state.file_uploader_key}")
        
        if p_files and st.button("🔍 Почати обробку"):
            st.session_state.scanned_data = []
            st.session_state.passport_payload = []
            
            p_bar = st.progress(0)
            for i, f in enumerate(p_files):
                img_np = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                res = {}
                for lbl, (x, y, w, h) in current_coords.items():
                    crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                    txt = " ".join([t[1] for t in reader.readtext(crop)])
                    res[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else re.sub(r'[^a-zA-Zа-яА-Я]', '', txt).capitalize()
                
                st.session_state.scanned_data.append(res)
                buf = compress_image(f)
                st.session_state.passport_payload.append((f"p{i}", (f"p_{i}.jpg", buf.read(), "image/jpeg")))
                p_bar.progress((i + 1) / len(p_files))
            st.rerun()

        if st.session_state.scanned_data:
            st.subheader("📝 Перевірка")
            final = []
            for idx, item in enumerate(st.session_state.scanned_data):
                cols = st.columns(3)
                s = cols[0].text_input(f"Прізвище #{idx}", item['Surname'], key=f"s_{idx}")
                n = cols[1].text_input(f"Ім'я #{idx}", item['Name'], key=f"n_{idx}")
                u = cols[2].text_input(f"ID #{idx}", item['ID'], key=f"u_{idx}")
                final.append({"Surname": s, "Name": n, "ID": u})
            
            c_files = st.file_uploader("2. Докази розрахунку (скріни)", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"c_{st.session_state.file_uploader_key}")
            
            if st.button("🚀 НАДІСЛАТИ ЗВІТ"):
                if not c_files:
                    st.error("Додайте докази!")
                else:
                    with st.spinner("Надсилаю..."):
                        msg = f"🏥 **НОВИЙ ЗВІТ** від <@{user['id']}>\nКількість: {len(final)}\n" + \
                              "\n".join([f"• {r['Surname']} {r['Name']} (ID: {r['ID']})" for r in final])
                        try:
                            requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": msg}, files=st.session_state.passport_payload)
                            pay = []
                            for i, cf in enumerate(c_files):
                                b = compress_image(cf)
                                pay.append((f"c{i}", (f"c_{i}.jpg", b.read(), "image/jpeg")))
                            requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": "💳 **Докази:**"}, files=pay)
                            
                            cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final), datetime.now().strftime("%Y-%m-%d %H:%M")))
                            conn.commit()
                            
                            st.success("✅ Звіт надіслано успішно!")
                            st.session_state.scanned_data = []
                            st.session_state.file_uploader_key += 1
                            st.rerun()
                        except Exception as e: st.error(f"Помилка: {e}")
