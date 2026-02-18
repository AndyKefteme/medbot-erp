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

# --- 0. ТЕХНІЧНІ НАЛАШТУВАННЯ ---
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Шлях до Tesseract на Linux (Streamlit Cloud)
if os.path.exists('/usr/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

def auth_log(msg, mode="info"):
    t = datetime.now().strftime("%H:%M:%S")
    if mode == "info": st.info(f"ℹ️ [{t}] {msg}")
    elif mode == "err": st.error(f"❌ [{t}] {msg}")
    elif mode == "ok": st.success(f"✅ [{t}] {msg}")

# --- 1. ПЕРЕВІРКА SECRETS ---
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
    st.error(f"Критична помилка: Перевірте налаштування Secrets у Streamlit Cloud! ({e})")
    st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- 2. БАЗА ДАНИХ (SQLite) ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 3. ФУНКЦІЇ ОБРОБКИ ТЕКСТУ ТА ФОТО ---
def ocr_process(image_np, is_id=False):
    try:
        gray = cv2.cvtColor(image_np, cv2.COLOR_RGB2GRAY)
        # Покращення контрасту для дрібного тексту
        _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        txt = pytesseract.image_to_string(thresh, config='--psm 7')
        if is_id:
            return "".join(re.findall(r'\d+', txt))
        return re.sub(r'[^a-zA-Zа-яА-ЯіїєґІЇЄҐ]', '', txt).strip().capitalize()
    except:
        return ""

def compress_image(image_file):
    img = Image.open(image_file).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75)
    buf.seek(0)
    return buf

# --- 4. СИСТЕМА АВТОРИЗАЦІЇ DISCORD ---
if 'auth_user' not in st.session_state: st.session_state.auth_user = None
if 'scanned_data' not in st.session_state: st.session_state.scanned_data = []
if 'passport_payload' not in st.session_state: st.session_state.passport_payload = []
if 'file_uploader_key' not in st.session_state: st.session_state.file_uploader_key = 0

def handle_discord_login():
    st.markdown("<h1 style='text-align: center;'>🏥 MedBot ERP System</h1>", unsafe_allow_html=True)
    
    with st.expander("🔍 Статус авторизації (Логи)", expanded=True):
        auth_log("Очікування натискання на посилання...")
        qp = st.query_params
        if "code" in qp:
            auth_log("Код отримано! Спроба авторизації...", mode="ok")
            try:
                discord = OAuth2Session(config['DISCORD_CLIENT_ID'], 
                                        redirect_uri=config['DISCORD_REDIRECT_URI'], 
                                        scope=['identify', 'guilds.members.read'])
                token = discord.fetch_token('https://discord.com/api/oauth2/token', 
                                            client_secret=config['DISCORD_CLIENT_SECRET'], 
                                            code=qp["code"])
                u_data = discord.get('https://discord.com/api/users/@me').json()
                m_res = discord.get(f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member")
                
                if m_res.status_code == 200:
                    u_roles = m_res.json().get('roles', [])
                    is_adm = config['ADMIN_ROLE_ID'] in u_roles
                    if config['ALLOWED_ROLE_ID'] in u_roles or is_adm:
                        st.session_state.auth_user = {"id": u_data['id'], "username": u_data['username'], "is_admin": is_adm}
                        st.query_params.clear()
                        st.rerun()
                auth_log("У вас немає потрібної ролі на сервері Discord!", mode="err")
            except Exception as e:
                auth_log(f"Помилка OAuth: {str(e)}", mode="err")

    auth_url = f"https://discord.com/api/oauth2/authorize?client_id={config['DISCORD_CLIENT_ID']}&redirect_uri={requests.utils.quote(config['DISCORD_REDIRECT_URI'])}&response_type=code&scope=identify%20guilds%20guilds.members.read"
    
    st.markdown(f'''
        <div style="text-align: center; margin-top: 50px; padding: 30px; border: 2px dashed #5865F2; border-radius: 15px;">
            <a href="{auth_url}" target="_top" style="
                background-color: #5865F2; color: white; padding: 20px 45px; 
                text-decoration: none; border-radius: 10px; font-weight: bold; font-size: 24px; 
                display: inline-block; box-shadow: 0 4px 15px rgba(88,101,242,0.3);
            ">🔗 УВІЙТИ ЧЕРЕЗ DISCORD</a>
        </div>
    ''', unsafe_allow_html=True)

if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

# --- 5. ОСНОВНИЙ ІНТЕРФЕЙС ПІСЛЯ ВХОДУ ---
user = st.session_state.auth_user
st.sidebar.title(f"👤 {user['username']}")
menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмінка", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.auth_user = None
    st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Налаштування трафарету розпізнавання")
    f = st.file_uploader("Завантажте зразок паспорта для розмітки", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Виберіть поле, яке розмічаєте", ["Surname", "Name", "ID"])
        st.info(f"Виділіть область для поля: **{target}**")
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        
        if st.button("💾 Зберегти зону"):
            cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (user['id'],))
            row = cursor.fetchone()
            coords = json.loads(row[0]) if row else {"Surname": None, "Name": None, "ID": None}
            coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (user['id'], json.dumps(coords)))
            conn.commit()
            st.success(f"Зону для '{target}' збережено успішно!")

elif menu == "📄 Сканер":
    cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (user['id'],))
    row = cursor.fetchone()
    if not row:
        st.warning("⚠️ Спочатку налаштуйте зони розпізнавання в розділі 'Налаштування'!")
    else:
        st.header("📸 Масове розпізнавання паспортів")
        coords = json.loads(row[0])
        p_files = st.file_uploader("Крок 1: Завантажте фото паспортів", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"p_{st.session_state.file_uploader_key}")
        
        if p_files and st.button("🔍 Почати розпізнавання (OCR)"):
            st.session_state.scanned_data = []
            st.session_state.passport_payload = []
            progress = st.progress(0)
            for i, f in enumerate(p_files):
                img = Image.open(f).convert("RGB").resize((1920, 1080))
                img_np = np.array(img)
                res = {}
                for lbl, data in coords.items():
                    if data:
                        x, y, w, h = data
                        crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                        res[lbl] = ocr_process(crop, is_id=(lbl=="ID"))
                    else:
                        res[lbl] = ""
                st.session_state.scanned_data.append(res)
                st.session_state.passport_payload.append((f"p{i}", (f"p_{i}.jpg", compress_image(f).read(), "image/jpeg")))
                progress.progress((i + 1) / len(p_files))
            st.rerun()

        if st.session_state.scanned_data:
            st.subheader("📝 Перевірка та редагування даних")
            for idx, item in enumerate(st.session_state.scanned_data):
                with st.container():
                    cols = st.columns([1, 1, 1])
                    item['Surname'] = cols[0].text_input(f"Прізвище #{idx+1}", item['Surname'], key=f"s{idx}")
                    item['Name'] = cols[1].text_input(f"Ім'я #{idx+1}", item['Name'], key=f"n{idx}")
                    item['ID'] = cols[2].text_input(f"ID #{idx+1}", item['ID'], key=f"i{idx}")
                    st.divider()
            
            c_files = st.file_uploader("Крок 2: Завантажте фото чеків (докази)", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"c_{st.session_state.file_uploader_key}")
            
            if st.button("🚀 ВІДПРАВИТИ ВЕСЬ ЗВІТ"):
                with st.spinner("Надсилаю дані в Discord..."):
                    main_msg = f"🏥 **НОВИЙ ЗВІТ ERP**\nСпівробітник: <@{user['id']}>\nДата: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n**Список пацієнтів:**\n"
                    for r in st.session_state.scanned_data:
                        main_msg += f"• {r['Surname']} {r['Name']} (ID: {r['ID']})\n"
                    
                    # 1. Відправка повідомлення з паспортами
                    requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": main_msg}, files=st.session_state.passport_payload)
                    
                    # 2. Відправка чеків (якщо є)
                    if c_files:
                        c_pay = [(f"c{i}", (f"c_{i}.jpg", compress_image(cf).read(), "image/jpeg")) for i, cf in enumerate(c_files)]
                        requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": "💳 **Чеки до звіту:**"}, files=c_pay)
                    
                    # 3. Логування в БД
                    cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(st.session_state.scanned_data), datetime.now().strftime("%d.%m.%Y %H:%M")))
                    conn.commit()
                    
                    st.success("✅ Звіт успішно надіслано в Discord!")
                    st.session_state.scanned_data = []
                    st.session_state.file_uploader_key += 1
                    st.rerun()

elif menu == "📊 Адмінка" and user['is_admin']:
    st.header("📊 Журнал операцій (Admin)")
    logs = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 100").fetchall()
    if logs:
        st.table([{"Користувач": r[1], "К-сть записів": r[2], "Час": r[3]} for r in logs])
    else:
        st.info("Журнал поки що порожній.")
