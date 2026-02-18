import streamlit as st
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
from urllib.parse import quote

# --- 1. CONFIG ---
try:
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
except Exception as e:
    st.error(f"Config error: {e}")
    st.stop()

st.set_page_config(layout="wide", page_title="MedBot ERP", page_icon="🏥")

# --- 2. DB ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- 3. OCR ---
@st.cache_resource(show_spinner=False)
def get_reader():
    return easyocr.Reader(['en', 'uk'], gpu=False)

# --- 4. LOGIN ---
def show_login():
    c_id = str(config['DISCORD_CLIENT_ID']).strip()
    r_uri = str(config['DISCORD_REDIRECT_URI']).strip()
    
    # Створюємо чисте посилання
    auth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={c_id}"
        f"&redirect_uri={quote(r_uri, safe='')}"
        f"&response_type=code"
        f"&scope=identify%20guilds%20guilds.members.read"
    )

    st.title("🏥 MedBot ERP System")
    st.divider()
    
    col1, col2 = st.columns([1, 1])
    
    with col1:
        st.subheader("Вхід через Discord")
        # Метод 1: Покращена кнопка
        st.markdown(f"""
            <div style="margin: 20px 0;">
                <a href="{auth_url}" target="_self">
                    <button style="
                        background-color: #5865F2; 
                        color: white; 
                        border: none; 
                        padding: 20px 40px; 
                        font-size: 22px; 
                        font-weight: bold; 
                        border-radius: 10px; 
                        cursor: pointer;
                        width: 100%;
                        box-shadow: 0 4px 15px rgba(88,101,242,0.4);
                    ">
                        🔑 АВТОРИЗУВАТИСЬ
                    </button>
                </a>
            </div>
        """, unsafe_allow_html=True)
        
        st.write("---")
        st.warning("⚠️ Якщо кнопка вище не відкривається, скопіюйте це посилання:")
        st.code(auth_url)

    with col2:
        if 'reader' not in st.session_state:
            with st.spinner("Завантаження OCR..."):
                st.session_state.reader = get_reader()
        st.success("✅ Система готова")

    # ОБРОБКА CALLBACK
    params = st.query_params
    if "code" in params:
        code = params["code"]
        data = {
            'client_id': c_id,
            'client_secret': config['DISCORD_CLIENT_SECRET'],
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': r_uri
        }
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        
        res = requests.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
        
        if res.status_code == 200:
            token = res.json()['access_token']
            h = {"Authorization": f"Bearer {token}"}
            u_info = requests.get("https://discord.com/api/users/@me", headers=h).json()
            
            # Ролі
            g_id = config['GUILD_ID']
            m_res = requests.get(f"https://discord.com/api/users/@me/guilds/{g_id}/member", headers=h)
            
            if m_res.status_code == 200:
                roles = m_res.json().get('roles', [])
                is_admin = config['ADMIN_ROLE_ID'] in roles
                is_allowed = config['ALLOWED_ROLE_ID'] in roles or is_admin
                
                if is_allowed:
                    st.session_state.auth_user = {"id": u_info['id'], "username": u_info['username'], "is_admin": is_admin}
                    st.query_params.clear()
                    st.rerun()
                else:
                    st.error("🚫 Немає доступу.")
            else:
                st.error("❌ Ви не на сервері.")
        else:
            st.error(f"Помилка: {res.text}")

# Запуск
if 'auth_user' not in st.session_state:
    show_login()
    st.stop()

# --- 5. MAIN APP ---
user = st.session_state.auth_user
reader = st.session_state.reader

def get_coords(u_id):
    res = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    return json.loads(res[0]) if res else {"Surname": None, "Name": None, "ID": None}

u_coords = get_coords(user['id'])

st.sidebar.title(f"👤 {user['username']}")
page = st.sidebar.radio("Меню", ["📄 Сканер", "⚙️ Налаштування", "📊 Логи", "🚪 Вихід"])

if page == "🚪 Вихід":
    st.session_state.clear()
    st.rerun()

elif page == "⚙️ Налаштування":
    st.header("📐 Налаштування зон")
    f = st.file_uploader("Завантажте зразок", type=['jpg', 'png'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Поле", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='blue', return_type='box')
        if st.button("Зберегти"):
            u_coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (user['id'], json.dumps(u_coords)))
            conn.commit()
            st.success("Збережено!")

elif page == "📄 Сканер":
    if not all(u_coords.values()):
        st.warning("Налаштуйте зони.")
    else:
        st.header("📸 Сканування")
        up = st.file_uploader("Фото", accept_multiple_files=True)
        if up and st.button("🔍 Розпізнати"):
            res_list = []
            for f in up:
                img = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                d = {}
                for lbl, (x, y, w, h) in u_coords.items():
                    crop = img[int(y):int(y+h), int(x):int(x+w)]
                    txt = " ".join([t[1] for t in reader.readtext(crop)])
                    d[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else txt.strip().capitalize()
                res_list.append(d)
            st.session_state.scan_res = res_list
            st.rerun()

        if 'scan_res' in st.session_state:
            final = []
            for i, r in enumerate(st.session_state.scan_res):
                c1, c2, c3 = st.columns(3)
                s = c1.text_input(f"Прізвище {i}", r['Surname'], key=f"s{i}")
                n = c2.text_input(f"Ім'я {i}", r['Name'], key=f"n{i}")
                u = c3.text_input(f"ID {i}", r['ID'], key=f"u{i}")
                final.append({"Surname": s, "Name": n, "ID": u})
            
            if st.button("🚀 ВІДПРАВИТИ В DISCORD"):
                msg = f"🏥 **Звіт від** <@{user['id']}>\n" + "\n".join([f"• {x['Surname']} {x['Name']} ID:{x['ID']}" for x in final])
                requests.post(config['DISCORD_WEBHOOK_URL'], json={"content": msg})
                cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final), datetime.now().strftime("%d.%m %H:%M")))
                conn.commit()
                st.success("Надіслано!")
                del st.session_state.scan_res

elif page == "📊 Логи":
    if user['is_admin']:
        logs = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC").fetchall()
        st.table([{"Користувач": r[1], "К-сть": r[2], "Час": r[3]} for r in logs])
