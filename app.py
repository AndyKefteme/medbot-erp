import streamlit as st
import numpy as np
import easyocr
import requests
import sqlite3
import re
import io
import time
import json
from PIL import Image
from streamlit_cropper import st_cropper
from datetime import datetime, timedelta
from requests_oauthlib import OAuth2Session

# --- 1. КЛЮЧ ДОСТУПУ ДЛЯ CRON-JOB (Keep Alive) ---
# Налаштуйте в cron-job.org URL: https://ems-zvit.streamlit.app/?keepalive=1
# Це ПОВИННО бути на самому початку, щоб уникнути 303 Redirect
if st.query_params.get("keepalive") == "1":
    st.write("✅ System Active")
    st.stop()

# --- ІНІЦІАЛІЗАЦІЯ СТАНІВ СЕСІЇ ---
if 'auth_user' not in st.session_state:
    st.session_state.auth_user = None
if 'oauth_state' not in st.session_state:
    st.session_state.oauth_state = None
if 'scanned_data' not in st.session_state:
    st.session_state.scanned_data = []
if 'passport_payload' not in st.session_state:
    st.session_state.passport_payload = []

# --- БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY, user_name TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS whitelist (user_id TEXT PRIMARY KEY, user_name TEXT)') # БІЛИЙ СПИСОК
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS sessions (user_id TEXT PRIMARY KEY, token_data TEXT, expiry TEXT, is_admin INTEGER)')
conn.commit()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

@st.cache_resource
def load_ocr():
    return easyocr.Reader(['en'], gpu=False)

reader = load_ocr()

# --- ДОПОМІЖНІ ФУНКЦІЇ ---
def is_blacklisted(u_id):
    return cursor.execute("SELECT 1 FROM blacklist WHERE user_id = ?", (u_id,)).fetchone() is not None

def is_whitelisted(u_id):
    return cursor.execute("SELECT 1 FROM whitelist WHERE user_id = ?", (u_id,)).fetchone() is not None

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

# --- АВТОРИЗАЦІЯ ---
def handle_discord_login():
    code = st.query_params.get("code")
    
    # 1. Відновлення сесії з перевіркою ролей та Білого списку
    if not code and st.session_state.auth_user is None:
        saved = cursor.execute("SELECT user_id, token_data, expiry, is_admin FROM sessions LIMIT 1").fetchone()
        if saved and datetime.now() < datetime.strptime(saved[2], "%Y-%m-%d %H:%M:%S"):
            try:
                # Якщо юзер у Білому списку — пускаємо без перевірки Discord
                if is_whitelisted(saved[0]):
                    st.session_state.auth_user = {"id": saved[0], "username": "💎 VIP Користувач", "is_admin": bool(saved[3])}
                    return

                # Якщо не в Білому списку — перевіряємо ролі через API
                token = json.loads(saved[1])
                discord = OAuth2Session(st.secrets["DISCORD_CLIENT_ID"], token=token)
                m_res = discord.get(f"https://discord.com/api/users/@me/guilds/{st.secrets['GUILD_ID']}/member").json()
                
                u_id = m_res.get('user', {}).get('id')
                u_roles = m_res.get('roles', [])
                is_adm = st.secrets['ADMIN_ROLE_ID'] in u_roles
                is_allowed = st.secrets['ALLOWED_ROLE_ID'] in u_roles or is_adm
                
                if not is_allowed or is_blacklisted(u_id):
                    cursor.execute("DELETE FROM sessions WHERE user_id = ?", (saved[0],))
                    conn.commit()
                    st.rerun()

                st.session_state.auth_user = {"id": u_id, "username": m_res.get('nick') or m_res.get('user', {}).get('username'), "is_admin": is_adm}
                return
            except:
                cursor.execute("DELETE FROM sessions"); conn.commit()
                return

    # 2. Екран входу
    if not code and st.session_state.auth_user is None:
        discord = OAuth2Session(st.secrets["DISCORD_CLIENT_ID"], redirect_uri=st.secrets["DISCORD_REDIRECT_URI"], scope=["identify", "guilds.members.read"])
        auth_url, state = discord.authorization_url("https://discord.com/api/oauth2/authorize")
        st.session_state.oauth_state = state
        st.title("🏥 MedBot ERP System")
        st.link_button("🔑 УВІЙТИ ЧЕРЕЗ DISCORD", auth_url, type="primary")
        st.stop()

    # 3. Callback від Discord
    if code and st.session_state.auth_user is None:
        try:
            discord = OAuth2Session(st.secrets["DISCORD_CLIENT_ID"], redirect_uri=st.secrets["DISCORD_REDIRECT_URI"], state=st.session_state.oauth_state)
            token = discord.fetch_token("https://discord.com/api/oauth2/token", client_secret=st.secrets["DISCORD_CLIENT_SECRET"], code=code)
            m_res = discord.get(f"https://discord.com/api/users/@me/guilds/{st.secrets['GUILD_ID']}/member").json()
            u_id = m_res.get('user', {}).get('id')
            
            if is_blacklisted(u_id):
                st.error("🚫 Бан"); st.stop()

            u_roles = m_res.get('roles', [])
            is_adm = st.secrets['ADMIN_ROLE_ID'] in u_roles
            # Пускаємо якщо є роль АБО якщо юзер у Білому списку
            if st.secrets['ALLOWED_ROLE_ID'] in u_roles or is_adm or is_whitelisted(u_id):
                server_nick = m_res.get('nick') or m_res.get('user', {}).get('username')
                st.session_state.auth_user = {"id": u_id, "username": server_nick, "is_admin": is_adm}
                expiry = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute("REPLACE INTO sessions VALUES (?, ?, ?, ?)", (u_id, json.dumps(token), expiry, 1 if is_adm else 0))
                conn.commit()
                st.query_params.clear(); st.rerun()
            else:
                st.error("Немає доступу"); st.stop()
        except: st.error("Помилка авторизації"); st.stop()

handle_discord_login()
user = st.session_state.auth_user
current_coords = load_user_coords(user['id'])

# --- МЕНЮ ---
st.sidebar.title(f"👤 {user['username']}")
menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if menu == "🚪 Вихід":
    cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user['id'],))
    conn.commit()
    st.session_state.auth_user = None; st.rerun()

elif menu == "📊 Адмін-панель":
    if not user['is_admin']: st.warning("Немає прав.")
    else:
        t_logs, t_ban, t_white, t_users = st.tabs(["📝 Логи", "🚫 Бан", "💎 Білий список", "👥 Сесії"])
        with t_logs:
            st.table([{"Користувач": r[1], "К-сть": r[2], "Дата": r[3]} for r in cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 50").fetchall()])
        
        with t_ban:
            bid = st.text_input("Discord ID для Бану")
            if st.button("Забанити"):
                cursor.execute("REPLACE INTO blacklist VALUES (?, 'Banned')", (bid,))
                cursor.execute("DELETE FROM sessions WHERE user_id = ?", (bid,))
                conn.commit(); st.rerun()
            for b in cursor.execute("SELECT * FROM blacklist").fetchall():
                if st.button(f"Розбан {b[0]}", key=f"un_{b[0]}"):
                    cursor.execute("DELETE FROM blacklist WHERE user_id = ?", (b[0],)); conn.commit(); st.rerun()
        
        with t_white:
            wid = st.text_input("Discord ID для Білого Списку")
            wname = st.text_input("Нікнейм (для себе)")
            if st.button("Додати в VIP"):
                cursor.execute("REPLACE INTO whitelist VALUES (?, ?)", (wid, wname))
                conn.commit(); st.rerun()
            st.write("---")
            for w in cursor.execute("SELECT * FROM whitelist").fetchall():
                if st.button(f"Видалити {w[0]} ({w[1]})", key=f"wid_{w[0]}"):
                    cursor.execute("DELETE FROM whitelist WHERE user_id = ?", (w[0],)); conn.commit(); st.rerun()

        with t_users:
            for r in cursor.execute("SELECT user_id, is_admin FROM sessions").fetchall():
                st.code(f"ID: {r[0]} | Admin: {bool(r[1])}")

elif menu == "⚙️ Налаштування":
    st.header("📐 Трафарет")
    if st.button("🗑 Скинути все"):
        save_user_coords(user['id'], {"Surname": None, "Name": None, "ID": None}); st.rerun()
    f = st.file_uploader("Завантажте фото", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Зона", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        if st.button("💾 Зберегти"):
            new_c = current_coords
            new_c[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_user_coords(user['id'], new_c); st.rerun()

elif menu == "📄 Сканер":
    if not all(current_coords.values()): st.error("Налаштуйте трафарет!")
    else:
        p_files = st.file_uploader("1. Паспорти", accept_multiple_files=True)
        if p_files and st.button("🔍 Сканувати"):
            st.session_state.scanned_data, st.session_state.passport_payload = [], []
            for i, f in enumerate(p_files):
                img_np = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                res = {}
                for lbl, (x, y, w, h) in current_coords.items():
                    crop = img_np[int(y):int(y+h), int(x):int(x+w)]
                    txt = " ".join([t[1] for t in reader.readtext(crop)])
                    res[lbl] = "".join(re.findall(r'\d+', txt)) if lbl == "ID" else re.sub(r'[^a-zA-Zа-яА-ЯіїєґІЇЄҐ]', '', txt).capitalize()
                st.session_state.scanned_data.append(res)
                st.session_state.passport_payload.append((f"p{i}", (f"p_{i}.jpg", compress_image(f).read(), "image/jpeg")))
            st.rerun()

        if st.session_state.get('scanned_data'):
            final = []
            for idx, item in enumerate(st.session_state.scanned_data):
                cols = st.columns([3, 3, 2])
                s = cols[0].text_input(f"Прізвище #{idx+1}", item['Surname'], key=f"s_{idx}")
                n = cols[1].text_input(f"Ім'я #{idx+1}", item['Name'], key=f"n_{idx}")
                u = cols[2].text_input(f"ID #{idx+1}", item['ID'], key=f"u_{idx}")
                final.append({"Surname": s, "Name": n, "ID": u})
            
            c_files = st.file_uploader("2. Докази", accept_multiple_files=True)
            if st.button("🚀 ВІДПРАВИТИ"):
                if not c_files: st.error("Додайте докази!")
                else:
                    msg = f"🏥 **ЗВІТ**\n<@{user['id']}> | {user['username']}\nК-сть: {len(final)}\n\n" + "\n".join([f"• {r['Surname']} {r['Name']} #{r['ID']}" for r in final])
                    try:
                        requests.post(st.secrets["DISCORD_WEBHOOK_URL"], data={"content": msg}, files=st.session_state.passport_payload)
                        c_pay = [(f"c{i}", (f"c_{i}.jpg", compress_image(cf).read(), "image/jpeg")) for i, cf in enumerate(c_files)]
                        requests.post(st.secrets["DISCORD_WEBHOOK_URL"], data={"content": "💳 **Докази:**"}, files=c_pay)
                        cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final), datetime.now().strftime("%Y-%m-%d %H:%M")))
                        conn.commit(); st.success("✅ Надіслано!"); st.session_state.scanned_data = []; time.sleep(2); st.rerun()
                    except Exception as e: st.error(f"Помилка: {e}")
