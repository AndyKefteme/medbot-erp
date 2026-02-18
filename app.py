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

# --- 0. НАЛАШТУВАННЯ ТА ЛОГУВАННЯ ---
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Функція для виводу логів прямо в інтерфейс
def debug_log(msg, type="info"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    full_msg = f"[{timestamp}] {msg}"
    if type == "info": st.write(f"ℹ️ {full_msg}")
    elif type == "success": st.success(f"✅ {full_msg}")
    elif type == "error": st.error(f"❌ {full_msg}")

# Шлях до Tesseract
if os.path.exists('/usr/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

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
    st.error(f"Помилка Secrets: {e}")
    st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro")

# --- 2. АВТОРИЗАЦІЯ З ЛОГУВАННЯМ ---
if 'auth_user' not in st.session_state:
    st.session_state.auth_user = None

def handle_discord_login():
    st.title("🏥 MedBot ERP System")
    debug_log("Перевірка параметрів URL...")
    
    # Отримуємо параметри через query_params
    qp = st.query_params
    
    if "code" in qp:
        code = qp["code"]
        debug_log(f"Код отримано: {code[:10]}...", type="success")
        
        try:
            debug_log("Запуск OAuth2Session для обміну коду на токен...")
            discord = OAuth2Session(
                config['DISCORD_CLIENT_ID'], 
                redirect_uri=config['DISCORD_REDIRECT_URI'], 
                scope=['identify', 'guilds.members.read']
            )
            
            debug_log("Запит до Discord API (/token)...")
            token = discord.fetch_token(
                'https://discord.com/api/oauth2/token', 
                client_secret=config['DISCORD_CLIENT_SECRET'], 
                code=code
            )
            debug_log("Токен отримано успішно!", type="success")
            
            debug_log("Запит даних користувача (@me)...")
            u_data = discord.get('https://discord.com/api/users/@me').json()
            
            debug_log(f"Запит ролей для сервера {config['GUILD_ID']}...")
            m_res = discord.get(f"https://discord.com/api/users/@me/guilds/{config['GUILD_ID']}/member")
            
            if m_res.status_code == 200:
                m_data = m_res.json()
                u_roles = m_data.get('roles', [])
                is_adm = config['ADMIN_ROLE_ID'] in u_roles
                is_allowed = config['ALLOWED_ROLE_ID'] in u_roles or is_adm
                
                if is_allowed:
                    debug_log("Доступ дозволено, зберігаємо сесію.", type="success")
                    st.session_state.auth_user = {"id": u_data['id'], "username": u_data['username'], "is_admin": is_adm}
                    st.query_params.clear()
                    st.rerun()
                else:
                    debug_log("Доступ заборонено: роль не знайдена.", type="error")
                    st.write(f"Ваші ролі: `{u_roles}`")
            else:
                debug_log(f"Помилка сервера Discord: {m_res.status_code}", type="error")
                
        except Exception as e:
            debug_log(f"Критична помилка процесу: {str(e)}", type="error")
            st.exception(e)
        st.stop()

    # Якщо коду немає - малюємо кнопку
    auth_url = (f"https://discord.com/api/oauth2/authorize?client_id={config['DISCORD_CLIENT_ID']}&"
                f"redirect_uri={requests.utils.quote(config['DISCORD_REDIRECT_URI'])}&"
                f"response_type=code&scope=identify%20guilds.members.read")

    st.info("Очікування натискання кнопки...")
    
    # Використовуємо HTML-посилання для надійності
    st.markdown(f'''
        <div style="text-align: center; border: 2px solid #5865F2; padding: 20px; border-radius: 10px;">
            <p>Натисніть кнопку нижче. Вас має перенаправити на Discord.</p>
            <a href="{auth_url}" target="_top" style="
                background-color: #5865F2; color: white; padding: 15px 30px; 
                text-decoration: none; border-radius: 8px; font-weight: bold; 
                display: inline-block; font-size: 1.2em;
            ">🔑 АВТОРИЗУВАТИСЬ</a>
        </div>
    ''', unsafe_allow_html=True)
    
    # Додаткова перевірка Redirect URI
    st.caption(f"Ваш поточний Redirect URI: `{config['DISCORD_REDIRECT_URI']}`")

if not st.session_state.auth_user:
    handle_discord_login()
    st.stop()

st.success(f"Ви ввійшли як {st.session_state.auth_user['username']}")

# --- ГОЛОВНИЙ ІНТЕРФЕЙС ---
user = st.session_state.auth_user
st.sidebar.title(f"👤 {user['username']}")
menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "🚪 Вихід"])

if menu == "🚪 Вихід":
    st.session_state.auth_user = None
    st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Трафарет")
    f = st.file_uploader("Зразок", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Поле", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        if st.button("💾 Зберегти"):
            cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (user['id'],))
            saved = cursor.fetchone()
            coords = json.loads(saved[0]) if saved else {"Surname": None, "Name": None, "ID": None}
            coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (user['id'], json.dumps(coords)))
            conn.commit()
            st.success("Збережено!")

elif menu == "📄 Сканер":
    cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (user['id'],))
    saved = cursor.fetchone()
    if not saved:
        st.warning("⚠️ Спочатку налаштуйте трафарет!")
    else:
        st.header("📸 Сканер")
        coords = json.loads(saved[0])
        p_files = st.file_uploader("Паспорти", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"p_{st.session_state.file_uploader_key}")
        
        if p_files and st.button("🔍 Старт"):
            st.session_state.scanned_data = []
            st.session_state.passport_payload = []
            for i, f in enumerate(p_files):
                img = Image.open(f).convert("RGB").resize((1920, 1080))
                img_np = np.array(img)
                res = {}
                for lbl, (x, y, w, h) in coords.items():
                    if x is not None:
                        crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                        res[lbl] = ocr_process(crop, is_id=(lbl=="ID"))
                st.session_state.scanned_data.append(res)
                st.session_state.passport_payload.append((f"p{i}", (f"p_{i}.jpg", compress_image(f).read(), "image/jpeg")))
            st.rerun()

        if st.session_state.scanned_data:
            for idx, item in enumerate(st.session_state.scanned_data):
                cols = st.columns(3)
                item['Surname'] = cols[0].text_input(f"Прізвище #{idx+1}", item['Surname'], key=f"s{idx}")
                item['Name'] = cols[1].text_input(f"Ім'я #{idx+1}", item['Name'], key=f"n{idx}")
                item['ID'] = cols[2].text_input(f"ID #{idx+1}", item['ID'], key=f"i{idx}")
            
            c_files = st.file_uploader("Чеки", accept_multiple_files=True, type=['png','jpg','jpeg'], key=f"c_{st.session_state.file_uploader_key}")
            
            if st.button("🚀 ВІДПРАВИТИ"):
                report = f"🏥 **ЗВІТ** від <@{user['id']}>\n" + \
                         "\n".join([f"• {r['Surname']} {r['Name']} #{r['ID']}" for r in st.session_state.scanned_data])
                requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": report}, files=st.session_state.passport_payload)
                
                if c_files:
                    c_pay = []
                    for i, cf in enumerate(c_files):
                        c_pay.append((f"c{i}", (f"c_{i}.jpg", compress_image(cf).read(), "image/jpeg")))
                    requests.post(config['DISCORD_WEBHOOK_URL'], data={"content": "💳 **Чеки:**"}, files=c_pay)
                
                st.success("✅ Надіслано!")
                st.session_state.scanned_data = []
                st.session_state.file_uploader_key += 1
                st.rerun()

