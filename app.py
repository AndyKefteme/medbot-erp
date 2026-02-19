import streamlit as st
import numpy as np
import easyocr
import requests
import sqlite3
import re
import os
import io
import time
from PIL import Image
from streamlit_cropper import st_cropper
from datetime import datetime, timedelta
from requests_oauthlib import OAuth2Session

# --- БАЗА ДАНИХ (Зберігаємо сесії та координати) ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS sessions (user_id TEXT PRIMARY KEY, token_data TEXT, expiry TEXT)')
conn.commit()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

@st.cache_resource
def load_ocr():
    return easyocr.Reader(['en'], gpu=False)

reader = load_ocr()

# --- ДОПОМІЖНІ ФУНКЦІЇ ---
def save_user_coords(u_id, coords):
    import json
    cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (u_id, json.dumps(coords)))
    conn.commit()

def load_user_coords(u_id):
    import json
    saved = cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u_id,)).fetchone()
    if saved: return json.loads(saved[0])
    return {"Surname": None, "Name": None, "ID": None}

def compress_image(image_file):
    img = Image.open(image_file).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    buf.seek(0)
    return buf

# --- АВТОРИЗАЦІЯ ЧЕРЕЗ SECRETS ---
def handle_discord_login():
    code = st.query_params.get("code")
    
    if not code and st.session_state.get('auth_user') is None:
        # Спроба знайти існуючу сесію в БД
        saved = cursor.execute("SELECT user_id, token_data, expiry FROM sessions LIMIT 1").fetchone()
        if saved and datetime.now() < datetime.strptime(saved[2], "%Y-%m-%d %H:%M:%S"):
            # Тут ми просто "пропускаємо", але для надійності краще повторний логін
            pass

        discord = OAuth2Session(
            st.secrets["DISCORD_CLIENT_ID"],
            redirect_uri=st.secrets["DISCORD_REDIRECT_URI"],
            scope=["identify", "guilds", "guilds.members.read"]
        )
        auth_url, state = discord.authorization_url("https://discord.com/api/oauth2/authorize")
        st.session_state.oauth_state = state
        
        st.title("🏥 MedBot ERP System")
        st.link_button("🔑 УВІЙТИ ЧЕРЕЗ DISCORD", auth_url, type="primary")
        st.stop()

    if code and st.session_state.get('auth_user') is None:
        try:
            discord = OAuth2Session(st.secrets["DISCORD_CLIENT_ID"], 
                                    redirect_uri=st.secrets["DISCORD_REDIRECT_URI"],
                                    state=st.session_state.oauth_state)
            token = discord.fetch_token("https://discord.com/api/oauth2/token",
                                        client_secret=st.secrets["DISCORD_CLIENT_SECRET"],
                                        code=code)
            
            # Отримання даних про нікнейм на сервері
            m_url = f"https://discord.com/api/users/@me/guilds/{st.secrets['GUILD_ID']}/member"
            m_res = discord.get(m_url).json()
            
            # Твій запит: Використовуємо нік на сервері (nick)
            server_nick = m_res.get('nick') or m_res.get('user', {}).get('global_name') or m_res.get('user', {}).get('username')
            u_id = m_res.get('user', {}).get('id')
            u_roles = m_res.get('roles', [])
            
            is_adm = st.secrets['ADMIN_ROLE_ID'] in u_roles
            if st.secrets['ALLOWED_ROLE_ID'] in u_roles or is_adm:
                st.session_state.auth_user = {"id": u_id, "username": server_nick, "is_admin": is_adm}
                
                # Зберігаємо сесію в БД на 7 днів
                import json
                expiry = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute("REPLACE INTO sessions VALUES (?, ?, ?)", (u_id, json.dumps(token), expiry))
                conn.commit()
                
                st.query_params.clear()
                st.rerun()
            else:
                st.error("Доступ заборонено роллю.")
                st.stop()
        except Exception as e:
            st.error(f"Помилка входу: {e}")
            st.stop()

handle_discord_login()
user = st.session_state.auth_user
current_coords = load_user_coords(user['id'])

# --- МЕНЮ ТА ОФОРМЛЕННЯ ---
st.sidebar.title(f"👤 {user['username']}")
if user['is_admin']: st.sidebar.subheader("👑 Адміністратор")
else: st.sidebar.caption("🩺 Співробітник")

menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if menu == "🚪 Вихід":
    cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user['id'],))
    conn.commit()
    st.session_state.auth_user = None
    st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Трафарет")
    c1, c2, c3 = st.columns(3)
    c1.write(f"Прізвище: {'✅' if current_coords['Surname'] else '❌'}")
    c2.write(f"Ім'я: {'✅' if current_coords['Name'] else '❌'}")
    c3.write(f"ID: {'✅' if current_coords['ID'] else '❌'}")

    f = st.file_uploader("Завантажте зразок", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Зона", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        if st.button("💾 Зберегти"):
            new_c = current_coords
            new_c[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_user_coords(user['id'], new_c)
            st.success(f"Збережено координати для {target}")
            time.sleep(1)
            st.rerun()

elif menu == "📄 Сканер":
    if not all(current_coords.values()):
        st.warning("⚠️ Налаштуйте координати!")
    else:
        st.header("📸 Новий звіт")
        p_files = st.file_uploader("1. Паспорти", accept_multiple_files=True, type=['png','jpg','jpeg'])
        if p_files and st.button("🔍 Розпізнати"):
            st.session_state.scanned_data = []
            st.session_state.passport_payload = []
            for i, f in enumerate(p_files):
                img_np = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                res = {}
                for lbl, (x, y, w, h) in current_coords.items():
                    crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                    txt = " ".join([t[1] for t in reader.readtext(crop)])
                    res[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else re.sub(r'[^a-zA-Z]', '', txt).capitalize()
                st.session_state.scanned_data.append(res)
                buf = compress_image(f)
                st.session_state.passport_payload.append((f"p{i}", (f"p_{i}.jpg", buf.read(), "image/jpeg")))
            st.rerun()

        if st.session_state.get('scanned_data'):
            final = []
            for idx, item in enumerate(st.session_state.scanned_data):
                cols = st.columns([3, 3, 2])
                s = cols[0].text_input(f"Прізвище #{idx}", item['Surname'], key=f"s_{idx}")
                n = cols[1].text_input(f"Ім'я #{idx}", item['Name'], key=f"n_{idx}")
                u = cols[2].text_input(f"ID #{idx}", item['ID'], key=f"u_{idx}")
                final.append({"Surname": s, "Name": n, "ID": u})
            
            c_files = st.file_uploader("2. Докази", accept_multiple_files=True, type=['png','jpg','jpeg'])
            if st.button("🚀 ВІДПРАВИТИ ЗВІТ"):
                if not c_files: st.error("Додайте докази!")
                else:
                    # ФОРМАТ НІКУ ЯК ТИ ПРОСИВ
                    user_mention = f"<@{user['id']}>"
                    server_nick = user['username']
                    user_info_report = f"{user_mention} | {server_nick}"
                    
                    msg = f"🏥 **ЗВІТ**\n{user_info_report}\nК-сть: {len(final)}\n\n" + \
                          "\n".join([f"• {r['Surname']} {r['Name']} #{r['ID']}" for r in final])
                    
                    try:
                        requests.post(st.secrets["DISCORD_WEBHOOK_URL"], data={"content": msg}, files=st.session_state.passport_payload)
                        c_pay = []
                        for i, cf in enumerate(c_files):
                            c_buf = compress_image(cf)
                            c_pay.append((f"c{i}", (f"c_{i}.jpg", c_buf.read(), "image/jpeg")))
                        requests.post(st.secrets["DISCORD_WEBHOOK_URL"], data={"content": "💳 **Докази:**"}, files=c_pay)
                        
                        cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], server_nick, len(final), datetime.now().strftime("%Y-%m-%d %H:%M")))
                        conn.commit()
                        st.success("✅ Надіслано!")
                        st.session_state.scanned_data = []
                        time.sleep(2)
                        st.rerun()
                    except Exception as e: st.error(f"Помилка: {e}")
