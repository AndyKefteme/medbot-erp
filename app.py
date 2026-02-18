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

# --- КОНФІГУРАЦІЯ ЧЕРЕЗ SECRETS ---
try:
    config = st.secrets["discord"]
except Exception:
    st.error("Критична помилка: Налаштування 'secrets' не знайдені в Streamlit Cloud!")
    st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- ОПТИМІЗОВАНЕ ЗАВАНТАЖЕННЯ OCR ---
@st.cache_resource(show_spinner="Завантаження моделі розпізнавання (це триває 1-2 хв)...")
def load_ocr():
    # Створюємо папку для моделей, щоб вони не перекачувались постійно
    model_storage = os.path.join(os.getcwd(), "ocr_models")
    if not os.path.exists(model_storage):
        os.makedirs(model_storage)
    return easyocr.Reader(['en', 'uk'], gpu=False, model_storage_directory=model_storage)

reader = load_ocr()

# --- ФУНКЦІЇ ---
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
    st.info("Будь ласка, пройдіть авторизацію для входу в систему.")
    
    login_html = f'<a href="{auth_url}" target="_self" style="background-color: #5865F2; color: white; padding: 15px 30px; text-decoration: none; border-radius: 8px; font-weight: bold; display: inline-block;">🔑 Увійти через Discord</a>'
    st.markdown(login_html, unsafe_allow_html=True)
    
    qp = st.query_params
    if "code" in qp:
        try:
            token = discord.fetch_token('https://discord.com/api/oauth2/token', client_secret=config['DISCORD_CLIENT_SECRET'], code=qp['code'])
            u_data = discord.get('https://discord.com/api/users/@me').json()
            u_id = u_data['id']
            
            if cursor.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (u_id,)).fetchone():
                st.error("❌ Доступ заблоковано.")
                st.stop()

            m_url = f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member"
            m_data = discord.get(m_url).json()
            u_roles = m_data.get('roles', [])
            is_adm = config['ADMIN_ROLE_ID'] in u_roles
            is_allowed = config['ALLOWED_ROLE_ID'] in u_roles or is_adm
            
            if not is_allowed:
                st.error("❌ У вас немає доступу до цієї системи.")
                st.stop()

            st.session_state.auth_user = {"id": u_id, "username": u_data['username'], "is_admin": is_adm}
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Помилка OAuth: {e}")

if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

user = st.session_state.auth_user
current_coords = load_user_coords(user['id'])

# --- МЕНЮ ---
st.sidebar.title(f"👤 {user['username']}")
menu = st.sidebar.radio("Меню", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.auth_user = None
    st.rerun()

elif menu == "📊 Адмін-панель":
    if not user['is_admin']:
        st.error("Доступ лише для адміністраторів.")
    else:
        st.header("🛡 Адміністрування")
        t1, t2 = st.tabs(["📝 Логи", "🚫 Чорний список"])
        with t1:
            logs = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 100").fetchall()
            st.table([{"Користувач": r[1], "Кількість": r[2], "Час": r[3]} for r in logs])
        with t2:
            bid = st.text_input("Discord ID для бану")
            if st.button("Забанити"):
                cursor.execute("INSERT OR IGNORE INTO blacklist VALUES (?)", (bid,))
                conn.commit()
                st.success("Готово")
            for r in cursor.execute("SELECT user_id FROM blacklist").fetchall():
                st.code(r[0])

elif menu == "⚙️ Налаштування":
    st.header("📐 Налаштування трафарету")
    if st.button("⚠️ Скинути координати"):
        save_user_coords(user['id'], {"Surname": None, "Name": None, "ID": None})
        st.rerun()
    
    file = st.file_uploader("Завантажте зразок паспорта", type=['png','jpg','jpeg'])
    if file:
        img = Image.open(file).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Що налаштовуємо?", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        if st.button("💾 Зберегти зону"):
            new_c = current_coords
            new_c[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_user_coords(user['id'], new_c)
            st.success(f"Зону {target} збережено!")

elif menu == "📄 Сканер":
    if not all(current_coords.values()):
        st.warning("Спочатку налаштуйте зони в 'Налаштуваннях'")
    else:
        st.header("📸 Сканування")
        p_files = st.file_uploader("1. Паспорти", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"p_{st.session_state.file_uploader_key}")
        if p_files and st.button("🔍 Обробити фото"):
            st.session_state.scanned_data = []
            st.session_state.passport_payload = []
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
            st.rerun()

        if st.session_state.scanned_data:
            st.subheader("Редагування")
            final = []
            for idx, item in enumerate(st.session_state.scanned_data):
                c1, c2, c3 = st.columns(3)
                s = c1.text_input(f"Прізвище #{idx}", item['Surname'], key=f"s_{idx}")
                n = c2.text_input(f"Ім'я #{idx}", item['Name'], key=f"n_{idx}")
                u = c3.text_input(f"ID #{idx}", item['ID'], key=f"u_{idx}")
                final.append({"Surname": s, "Name": n, "ID": u})
            
            c_files = st.file_uploader("2. Чеки/Докази", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"c_{st.session_state.file_uploader_key}")
            if st.button("🚀 ВІДПРАВИТИ ЗВІТ"):
                if not c_files: st.error("Додайте докази!")
                else:
                    report = f"🏥 **ЗВІТ** від <@{user['id']}>\n" + "\n".join([f"• {r['Surname']} {r['Name']} (#{r['ID']})" for r in final])
                    try:
                        requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": report}, files=st.session_state.passport_payload)
                        pay = []
                        for i, cf in enumerate(c_files):
                            b = compress_image(cf)
                            pay.append((f"c{i}", (f"c_{i}.jpg", b.read(), "image/jpeg")))
                        requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": "💳 Докази розрахунку:"}, files=pay)
                        cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final), datetime.now().strftime("%Y-%m-%d %H:%M")))
                        conn.commit()
                        st.success("✅ Надіслано!")
                        st.session_state.scanned_data = []
                        st.session_state.file_uploader_key += 1
                        st.rerun()
                    except Exception as e: st.error(f"Помилка: {e}")
