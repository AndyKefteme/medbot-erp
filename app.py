import streamlit as st
import cv2
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

# --- БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
# Таблиця для збереження сесій користувачів
cursor.execute('CREATE TABLE IF NOT EXISTS sessions (user_id TEXT PRIMARY KEY, token_data TEXT, expiry TEXT)')
conn.commit()

st.set_page_config(layout="wide", page_title="MedBot ERP Pro", page_icon="🏥")

# --- OCR МОДЕЛЯ ---
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

# --- ЛОГІКА СЕСІЙ (Щоб не заходити щоразу) ---
def get_saved_user():
    saved = cursor.execute("SELECT user_id, token_data, expiry FROM sessions").fetchone()
    if saved:
        user_id, token_data, expiry = saved
        if datetime.now() < datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S"):
            # Тут в ідеалі перевірити роль через API знову, але для швидкості беремо з бази
            return user_id
    return None

# --- АВТОРИЗАЦІЯ ---
def handle_discord_login():
    code = st.query_params.get("code")
    
    if not code and st.session_state.get('auth_user') is None:
        # Перевіряємо, чи є активна сесія в БД
        saved_id = get_saved_user()
        if saved_id:
            # Спроба відновити дані (спрощено)
            st.session_state.auth_user = {"id": saved_id, "username": "Повернувся", "is_admin": False} 
            # Для повної безпеки тут треба зробити повторний запит до Discord API
        
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
            
            u_data = discord.get("https://discord.com/api/users/@me").json()
            
            # Отримуємо дані про сервер (Guild Member) для нікнейму та ролей
            m_url = f"https://discord.com/api/users/@me/guilds/{st.secrets['GUILD_ID']}/member"
            m_res = discord.get(m_url).json()
            
            # Пріоритет імені: Нік на сервері -> Глобальне ім'я -> Логін
            display_name = m_res.get('nick') or u_data.get('global_name') or u_data.get('username')
            u_roles = m_res.get('roles', [])
            is_adm = st.secrets['ADMIN_ROLE_ID'] in u_roles
            
            if st.secrets['ALLOWED_ROLE_ID'] in u_roles or is_adm:
                st.session_state.auth_user = {"id": u_data['id'], "username": display_name, "is_admin": is_adm}
                # Зберігаємо в БД на 7 днів
                expiry = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
                import json
                cursor.execute("REPLACE INTO sessions VALUES (?, ?, ?)", (u_data['id'], json.dumps(token), expiry))
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

# --- МЕНЮ ---
st.sidebar.title(f"👤 {user['username']}")
if user['is_admin']: st.sidebar.subheader("👑 Адміністратор")
else: st.sidebar.caption("🩺 Співробітник")

menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if menu == "🚪 Вихід":
    cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user['id'],))
    conn.commit()
    st.session_state.auth_user = None
    st.rerun()

elif menu == "📊 Адмін-панель":
    if not user['is_admin']:
        st.warning("Доступ закритий.")
    else:
        t_logs, t_users = st.tabs(["📝 Логи", "👥 Користувачі/Бан"])
        with t_logs:
            h = cursor.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT 50").fetchall()
            st.table([{"Користувач": r[1], "К-сть": r[2], "Дата": r[3]} for r in h])
        with t_users:
            st.subheader("Управління доступом")
            bid = st.text_input("Введіть Discord ID")
            if st.button("🚫 Забанити за ID"):
                cursor.execute("INSERT OR IGNORE INTO blacklist VALUES (?)", (bid,))
                conn.commit()
                st.success(f"ID {bid} додано в чорний список")
            
            st.write("---")
            st.write("**Чорний список:**")
            for r in cursor.execute("SELECT user_id FROM blacklist").fetchall():
                bc1, bc2 = st.columns([3, 1])
                bc1.code(r[0])
                if bc2.button("🗑 Розбанити", key=f"b_{r[0]}"):
                    cursor.execute("DELETE FROM blacklist WHERE user_id = ?", (r[0],))
                    conn.commit()
                    st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Налаштування трафарету")
    
    # Вивід статусів збереження (ГАЛОЧКИ)
    c1, c2, c3 = st.columns(3)
    c1.write(f"Прізвище: {'✅' if current_coords['Surname'] else '❌'}")
    c2.write(f"Ім'я: {'✅' if current_coords['Name'] else '❌'}")
    c3.write(f"ID: {'✅' if current_coords['ID'] else '❌'}")

    if st.button("🗑 Скинути все"):
        save_user_coords(user['id'], {"Surname": None, "Name": None, "ID": None})
        st.rerun()

    f = st.file_uploader("Завантажте фото для налаштування", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Що налаштовуємо?", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        
        if st.button("💾 Зберегти координати"):
            new_c = current_coords
            new_c[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            save_user_coords(user['id'], new_c)
            st.success(f"✅ Координати для {target} успішно збережені!")
            time.sleep(1)
            st.rerun()

elif menu == "📄 Сканер":
    if not all(current_coords.values()):
        st.warning("⚠️ Спочатку налаштуйте всі 3 зони в Налаштуваннях!")
    else:
        st.header("📸 Створення звіту")
        p_files = st.file_uploader("1. Фото паспортів", accept_multiple_files=True, type=['png','jpg','jpeg'])
        
        if p_files and st.button("🔍 Почати зчитування"):
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
            st.subheader("📝 Перевірка даних")
            final = []
            for idx, item in enumerate(st.session_state.scanned_data):
                cols = st.columns([3, 3, 2])
                s = cols[0].text_input(f"Прізвище #{idx+1}", item['Surname'], key=f"s_{idx}")
                n = cols[1].text_input(f"Ім'я #{idx+1}", item['Name'], key=f"n_{idx}")
                u = cols[2].text_input(f"ID #{idx+1}", item['ID'], key=f"u_{idx}")
                final.append({"Surname": s, "Name": n, "ID": u})
            
            c_files = st.file_uploader("2. Докази оплати", accept_multiple_files=True, type=['png','jpg','jpeg'])
            if st.button("🚀 ВІДПРАВИТИ ЗВІТ"):
                if not c_files:
                    st.error("Додайте скріншоти оплати!")
                else:
                    # ФОРМУВАННЯ НІКНЕЙМУ ЯК НА СЕРВЕРІ
                    msg = f"🏥 **ЗВІТ**\nАвтор: <@{user['id']}> (**{user['username']}**)\nК-сть: {len(final)}\n\n" + \
                          "\n".join([f"• {r['Surname']} {r['Name']} #{r['ID']}" for r in final])
                    
                    try:
                        requests.post(st.secrets["DISCORD_WEBHOOK_URL"], data={"content": msg}, files=st.session_state.passport_payload)
                        c_pay = []
                        for i, cf in enumerate(c_files):
                            c_buf = compress_image(cf)
                            c_pay.append((f"c{i}", (f"c_{i}.jpg", c_buf.read(), "image/jpeg")))
                        requests.post(st.secrets["DISCORD_WEBHOOK_URL"], data={"content": "💳 **Докази оплати:**"}, files=c_pay)
                        
                        cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final), datetime.now().strftime("%Y-%m-%d %H:%M")))
                        conn.commit()
                        st.success("✅ Звіт успішно надіслано в Дискорд!")
                        st.session_state.scanned_data = []
                        time.sleep(2)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Помилка відправки: {e}")
