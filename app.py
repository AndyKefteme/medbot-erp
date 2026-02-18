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

# --- КОНФІГУРАЦІЯ (Береться з Secrets Streamlit) ---
try:
    config = st.secrets["discord"]
except Exception:
    st.error("Помилка: Налаштування 'secrets' не знайдено в панелі керування Streamlit!")
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

# --- АВТОРИЗАЦІЯ DISCORD ---
def handle_discord_login():
    scope = ['identify', 'guilds', 'guilds.members.read']
    # Redirect URI має збігатися з тим, що вказано в Discord Developer Portal
    discord = OAuth2Session(config['DISCORD_CLIENT_ID'], redirect_uri=config['DISCORD_REDIRECT_URI'], scope=scope)
    auth_url, _ = discord.authorization_url('https://discord.com/api/oauth2/authorize')
    
    st.title("🏥 MedBot ERP System")
    st.info("Будь ласка, авторизуйтесь через Discord для доступу.")
    
    login_html = f'''
        <a href="{auth_url}" target="_self" style="
            background-color: #5865F2; color: white; padding: 12px 24px; 
            text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block;
        ">🔑 Увійти через Discord</a>
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
            
            # Перевірка чорного списку
            if cursor.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (u_id,)).fetchone():
                st.error("❌ Ваш доступ заблоковано адміністратором.")
                st.stop()

            # Перевірка ролей на сервері
            m_url = f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member"
            m_data = discord.get(m_url).json()
            u_roles = m_data.get('roles', [])
            
            is_adm = config['ADMIN_ROLE_ID'] in u_roles
            is_allowed = config['ALLOWED_ROLE_ID'] in u_roles or is_adm
            
            if not is_allowed:
                st.error("❌ У вас немає дозволеної ролі на сервері Discord.")
                st.stop()

            st.session_state.auth_user = {"id": u_id, "username": u_data['username'], "is_admin": is_adm}
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Помилка авторизації: {e}")

# Перевірка логіну
if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

user = st.session_state.auth_user
current_coords = load_user_coords(user['id'])

# --- ОСНОВНЕ МЕНЮ ---
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
        t_logs, t_ban = st.tabs(["📝 Останні звіти", "🚫 Керування доступом"])
        with t_logs:
            logs = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 50").fetchall()
            st.table([{"Користувач": r[1], "Звітів": r[2], "Час": r[3]} for r in logs])
        with t_ban:
            bid = st.text_input("Введіть Discord ID для блокування")
            if st.button("Заблокувати"):
                cursor.execute("INSERT OR IGNORE INTO blacklist VALUES (?)", (bid,))
                conn.commit()
                st.success("Користувача заблоковано")
            
            st.subheader("Чорний список")
            for r in cursor.execute("SELECT user_id FROM blacklist").fetchall():
                bc1, bc2 = st.columns([3, 1])
                bc1.code(r[0])
                if bc2.button("Видалити", key=f"ban_{r[0]}"):
                    cursor.execute("DELETE FROM blacklist WHERE user_id = ?", (r[0],))
                    conn.commit()
                    st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Налаштування трафарету")
    st.info("Виберіть зону та виділіть її на зображенні. Це потрібно зробити один раз.")
    
    if st.button("🗑 Скинути всі координати"):
        save_user_coords(user['id'], {"Surname": None, "Name": None, "ID": None})
        st.rerun()

    f = st.file_uploader("Завантажте фото паспорта як зразок", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Що виділяємо?", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        
        if st.button(f"Зберегти зону для {target}"):
            new_c = current_coords
            new_c[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_user_coords(user['id'], new_c)
            st.success(f"Зону {target} збережено!")

elif menu == "📄 Сканер":
    if not all(current_coords.values()):
        st.warning("⚠️ Спочатку налаштуйте зони сканування в розділі 'Налаштування'!")
    else:
        st.header("📸 Сканування паспортів")
        p_files = st.file_uploader("1. Завантажте фото паспортів", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"p_{st.session_state.file_uploader_key}")
        
        if p_files and st.button("🔍 Почати розпізнавання"):
            st.session_state.scanned_data = []
            st.session_state.passport_payload = []
            
            progress = st.progress(0)
            for i, f in enumerate(p_files):
                img_pil = Image.open(f).convert("RGB").resize((1920, 1080))
                img_np = np.array(img_pil)
                res = {}
                
                for lbl, (x, y, w, h) in current_coords.items():
                    crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                    # Виконуємо OCR
                    txt_list = reader.readtext(crop)
                    txt = " ".join([t[1] for t in txt_list])
                    
                    if lbl == "ID":
                        res[lbl] = "".join(re.findall(r'\d+', txt))
                    else:
                        res[lbl] = re.sub(r'[^a-zA-Zа-яА-Я]', '', txt).capitalize()
                
                st.session_state.scanned_data.append(res)
                # Готуємо файл для відправки в Discord
                buf = compress_image(f)
                st.session_state.passport_payload.append((f"p{i}", (f"p_{i}.jpg", buf.read(), "image/jpeg")))
                progress.progress((i + 1) / len(p_files))
            st.rerun()

        if st.session_state.scanned_data:
            st.subheader("📝 Перевірка та редагування результатів")
            final_list = []
            for idx, item in enumerate(st.session_state.scanned_data):
                c1, c2, c3 = st.columns(3)
                s = c1.text_input(f"Прізвище #{idx+1}", item['Surname'], key=f"s_{idx}")
                n = c2.text_input(f"Ім'я #{idx+1}", item['Name'], key=f"n_{idx}")
                u = c3.text_input(f"ID #{idx+1}", item['ID'], key=f"u_{idx}")
                final_list.append({"Surname": s, "Name": n, "ID": u})
            
            st.divider()
            c_files = st.file_uploader("2. Додайте скріншоти розрахунку", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"c_{st.session_state.file_uploader_key}")
            
            if st.button("🚀 ВІДПРАВИТИ ЗВІТ У DISCORD"):
                if not c_files:
                    st.error("Будь ласка, додайте скріншоти розрахунку!")
                else:
                    with st.spinner("Відправка..."):
                        # Формуємо текст звіту
                        report_text = f"🏥 **НОВИЙ ЗВІТ ВІД ПРАЦІВНИКА**\n" \
                                      f"👤 Користувач: <@{user['id']}> ({user['username']})\n" \
                                      f"📊 Кількість людей: {len(final_list)}\n" \
                                      f"━━━━━━━━━━━━━━━━━━\n"
                        for p in final_list:
                            report_text += f"🔹 {p['Surname']} {p['Name']} — ID: `{p['ID']}`\n"
                        
                        try:
                            # 1. Відправляємо текст і фото паспортів
                            requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": report_text}, files=st.session_state.passport_payload)
                            
                            # 2. Відправляємо чеки
                            checks_payload = []
                            for i, cf in enumerate(c_files):
                                c_buf = compress_image(cf)
                                checks_payload.append((f"c{i}", (f"c_{i}.jpg", c_buf.read(), "image/jpeg")))
                            
                            requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": "💳 **Скріншоти розрахунку:**"}, files=checks_payload)
                            
                            # Запис у логи
                            cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final_list), datetime.now().strftime("%Y-%m-%d %H:%M")))
                            conn.commit()
                            
                            st.success("✅ Звіт успішно надіслано!")
                            # Очищення
                            st.session_state.scanned_data = []
                            st.session_state.file_uploader_key += 1
                            st.rerun()
                        except Exception as e:
                            st.error(f"Помилка при відправці: {e}")
