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

# --- 1. ПІДГОТОВКА СЕРВЕРНОГО СЕРЕДОВИЩА ---
# Створюємо локальну папку для моделей, щоб не залежати від системних шляхів
MODEL_PATH = os.path.join(os.getcwd(), "ocr_models")
if not os.path.exists(MODEL_PATH):
    os.makedirs(MODEL_PATH)

# --- 2. КОНФІГУРАЦІЯ SECRETS ---
try:
    config = st.secrets["discord"]
except Exception:
    st.error("Налаштуйте 'Secrets' у Streamlit Cloud (Settings -> Secrets)!")
    st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- 3. ФУНКЦІЯ ЗАВАНТАЖЕННЯ З ПРОГРЕСОМ ---
@st.cache_resource(show_spinner=False)
def load_ocr_engine():
    placeholder = st.empty()
    with placeholder.container():
        st.markdown("### 🤖 Підготовка штучного інтелекту")
        st.info("Завантаження моделей розпізнавання тексту. Перший запуск триває 2-4 хвилини...")
        
        # Смуга прогресу (імітація, оскільки easyocr не дає зворотного зв'язку)
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # Крок 1: Початок
        status_text.text("З'єднання з сервером моделей...")
        progress_bar.progress(15)
        
        # Крок 2: Ініціалізація (саме тут відбувається основне завантаження)
        # model_storage_directory змушує EasyOCR качати моделі в нашу папку
        reader = easyocr.Reader(['en', 'uk'], gpu=False, model_storage_directory=MODEL_PATH)
        
        progress_bar.progress(90)
        status_text.text("Фіналізація моделей...")
        time.sleep(1)
        
        progress_bar.progress(100)
        st.success("✅ Систему активовано!")
        time.sleep(1)
    
    placeholder.empty()
    return reader

# Виклик ініціалізації
reader = load_ocr_engine()

# --- 4. БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 5. ДОПОМІЖНІ ФУНКЦІЇ ---
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

# --- 6. СТАН СЕСІЇ ---
if 'auth_user' not in st.session_state: st.session_state.auth_user = None
if 'scanned_data' not in st.session_state: st.session_state.scanned_data = []
if 'passport_payload' not in st.session_state: st.session_state.passport_payload = []
if 'file_uploader_key' not in st.session_state: st.session_state.file_uploader_key = 0

# --- 7. АВТОРИЗАЦІЯ DISCORD ---
def handle_discord_login():
    scope = ['identify', 'guilds', 'guilds.members.read']
    discord = OAuth2Session(config['DISCORD_CLIENT_ID'], redirect_uri=config['DISCORD_REDIRECT_URI'], scope=scope)
    auth_url, _ = discord.authorization_url('https://discord.com/api/oauth2/authorize')
    
    st.title("🏥 MedBot ERP System")
    st.write("---")
    
    # Центрування кнопки через колони
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown(f'''
            <div style="text-align: center;">
                <p>Для роботи з системою необхідно підтвердити особу через Discord</p>
                <a href="{auth_url}" target="_self" style="
                    background-color: #5865F2; color: white; padding: 18px 40px; 
                    text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 20px;
                    display: inline-block; box-shadow: 0 4px 15px rgba(88,101,242,0.4);
                ">🔑 УВІЙТИ ЧЕРЕЗ DISCORD</a>
            </div>
        ''', unsafe_allow_html=True)
    
    qp = st.query_params
    if "code" in qp:
        try:
            token = discord.fetch_token('https://discord.com/api/oauth2/token', 
                                        client_secret=config['DISCORD_CLIENT_SECRET'], 
                                        code=qp['code'])
            u_data = discord.get('https://discord.com/api/users/@me').json()
            u_id = u_data['id']
            
            if cursor.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (u_id,)).fetchone():
                st.error("🚫 Доступ заблоковано.")
                st.stop()

            m_url = f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member"
            m_data = discord.get(m_url).json()
            u_roles = m_data.get('roles', [])
            
            is_adm = config['ADMIN_ROLE_ID'] in u_roles
            is_allowed = config['ALLOWED_ROLE_ID'] in u_roles or is_adm
            
            if not is_allowed:
                st.error("🚫 Відсутня необхідна роль у Discord.")
                st.stop()

            st.session_state.auth_user = {"id": u_id, "username": u_data['username'], "is_admin": is_adm}
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Помилка авторизації: {e}")

if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

# --- 8. ОСНОВНА ЧАСТИНА ДОДАТКА ---
user = st.session_state.auth_user
current_coords = load_user_coords(user['id'])

st.sidebar.title(f"👤 {user['username']}")
menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.auth_user = None
    st.rerun()

elif menu == "📊 Адмін-панель":
    if not user['is_admin']:
        st.warning("Доступ тільки для адміністрації.")
    else:
        st.header("🛡 Адміністрування")
        t_logs, t_ban = st.tabs(["📝 Журнал звітів", "🚫 Керування доступом"])
        with t_logs:
            data = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 100").fetchall()
            st.table([{"Користувач": r[1], "К-сть документів": r[2], "Дата/Час": r[3]} for r in data])
        with t_ban:
            bid = st.text_input("Discord ID користувача")
            if st.button("Заблокувати ID"):
                cursor.execute("INSERT OR IGNORE INTO blacklist VALUES (?)", (bid,))
                conn.commit()
                st.success(f"ID {bid} заблоковано")
            
            st.subheader("Список заблокованих")
            for r in cursor.execute("SELECT user_id FROM blacklist").fetchall():
                c1, c2 = st.columns([4, 1])
                c1.code(r[0])
                if c2.button("Розблокувати", key=f"unban_{r[0]}"):
                    cursor.execute("DELETE FROM blacklist WHERE user_id = ?", (r[0],))
                    conn.commit()
                    st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Калібрування сканера")
    st.info("Завантажте зразок фото та виділіть відповідні зони для розпізнавання.")
    
    if st.button("🗑 Очистити налаштування зон"):
        save_user_coords(user['id'], {"Surname": None, "Name": None, "ID": None})
        st.rerun()
    
    f = st.file_uploader("Завантажте фото-приклад", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Яку зону виділяємо?", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        
        if st.button(f"Зберегти координати для {target}"):
            new_c = current_coords
            new_c[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_user_coords(user['id'], new_c)
            st.success(f"Зону {target} налаштовано!")

elif menu == "📄 Сканер":
    if not all(current_coords.values()):
        st.warning("⚠️ Спочатку налаштуйте всі зони (Прізвище, Ім'я, ID) у вкладці 'Налаштування'!")
    else:
        st.header("📸 Робота зі звітом")
        p_files = st.file_uploader("1. Завантажте фото паспортів", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"p_{st.session_state.file_uploader_key}")
        
        if p_files and st.button("🔍 Розпізнати текст"):
            st.session_state.scanned_data = []
            st.session_state.passport_payload = []
            
            p_bar = st.progress(0)
            for i, f in enumerate(p_files):
                img_np = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                res = {}
                for lbl, (x, y, w, h) in current_coords.items():
                    crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                    txt_data = reader.readtext(crop)
                    txt = " ".join([t[1] for t in txt_data])
                    
                    if lbl == "ID":
                        res[lbl] = "".join(re.findall(r'\d+', txt))
                    else:
                        res[lbl] = re.sub(r'[^a-zA-Zа-яА-Я]', '', txt).capitalize()
                
                st.session_state.scanned_data.append(res)
                buf = compress_image(f)
                st.session_state.passport_payload.append((f"p{i}", (f"p_{i}.jpg", buf.read(), "image/jpeg")))
                p_bar.progress((i + 1) / len(p_files))
            st.rerun()

        if st.session_state.scanned_data:
            st.subheader("📝 Перевірка та коригування")
            final_data = []
            for idx, item in enumerate(st.session_state.scanned_data):
                col1, col2, col3 = st.columns(3)
                s = col1.text_input(f"Прізвище #{idx+1}", item['Surname'], key=f"s_{idx}")
                n = col2.text_input(f"Ім'я #{idx+1}", item['Name'], key=f"n_{idx}")
                u = col3.text_input(f"ID #{idx+1}", item['ID'], key=f"u_{idx}")
                final_data.append({"Surname": s, "Name": n, "ID": u})
            
            st.write("---")
            c_files = st.file_uploader("2. Завантажте докази розрахунку", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"c_{st.session_state.file_uploader_key}")
            
            if st.button("🚀 ВІДПРАВИТИ ЗВІТ У DISCORD"):
                if not c_files:
                    st.error("Додайте хоча б один скріншот розрахунку!")
                else:
                    with st.spinner("Надсилання даних..."):
                        content = f"🏥 **НОВИЙ ЗВІТ**\n👤 Від: <@{user['id']}>\n📊 Кількість: {len(final_data)}\n" + \
                                  "━━━━━━━━━━━━━━━━━━\n" + \
                                  "\n".join([f"🔹 {r['Surname']} {r['Name']} (ID: `{r['ID']}`)" for r in final_data])
                        try:
                            # Відправка паспортів
                            requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": content}, files=st.session_state.passport_payload)
                            
                            # Відправка чеків
                            checks = []
                            for i, cf in enumerate(c_files):
                                cb = compress_image(cf)
                                checks.append((f"c{i}", (f"c_{i}.jpg", cb.read(), "image/jpeg")))
                            requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": "💳 **Докази розрахунку:**"}, files=checks)
                            
                            # Логування
                            cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final_data), datetime.now().strftime("%Y-%m-%d %H:%M")))
                            conn.commit()
                            
                            st.success("✅ Звіт успішно надіслано в Discord!")
                            st.session_state.scanned_data = []
                            st.session_state.file_uploader_key += 1
                            st.rerun()
                        except Exception as e:
                            st.error(f"Помилка при надсиланні: {e}")
