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

# --- КОНФІГУРАЦІЯ СТОРІНКИ (Приховуємо вихідний код) ---
st.set_page_config(
    layout="wide", 
    page_title="MedBot ERP Pro", 
    page_icon="🏥",
    initial_sidebar_state="expanded"
)

# Стилі для приховування кнопок GitHub, Deploy та меню
st.markdown("""
    <style>
    #MainMenu {visibility: hidden;}
    header {visibility: hidden;}
    footer {visibility: hidden;}
    .stDeployButton {display:none;}
    [data-testid="stToolbar"] {visibility: hidden !important;}
    </style>
""", unsafe_allow_html=True)

# --- 1. ПЕРЕВІРКА ДЛЯ CRON-JOB ---
if st.query_params.get("keepalive") == "1":
    st.write("Server is active")
    st.stop()

# --- ІНІЦІАЛІЗАЦІЯ СТАНІВ ---
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
cursor.execute('CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT PRIMARY KEY, user_name TEXT, reason TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS whitelist (user_id TEXT PRIMARY KEY, user_name TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS sessions (user_id TEXT PRIMARY KEY, user_name TEXT, token_data TEXT, expiry TEXT, is_admin INTEGER)')
conn.commit()

@st.cache_resource
def load_ocr():
    return easyocr.Reader(['en', 'uk'], gpu=False)
reader = load_ocr()

# --- ДОПОМІЖНІ ФУНКЦІЇ ---
def is_blacklisted(u_id):
    res = cursor.execute("SELECT reason FROM blacklist WHERE user_id = ?", (u_id,)).fetchone()
    return res[0] if res else None

def is_whitelisted(u_id):
    res = cursor.execute("SELECT 1 FROM whitelist WHERE user_id = ?", (u_id,)).fetchone()
    return res is not None

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

# --- АВТОРИЗАЦІЯ ---
def handle_discord_login():
    # 1. Якщо користувач натиснув "Вийти", або ми хочемо скинути сесію через помилку
    if st.query_params.get("reset") == "1":
        st.session_state.auth_user = None
        st.session_state.oauth_state = None
        st.query_params.clear()
        st.rerun()

    code = st.query_params.get("code")
    
    # 2. Якщо користувач вже авторизований у цій вкладці — просто працюємо
    if st.session_state.get('auth_user'):
        return

    # 3. Якщо немає коду і юзер не в системі — показуємо кнопку входу
    if not code:
        discord = OAuth2Session(
            st.secrets["DISCORD_CLIENT_ID"], 
            redirect_uri=st.secrets["DISCORD_REDIRECT_URI"], 
            scope=["identify", "guilds.members.read"]
        )
        auth_url, state = discord.authorization_url("https://discord.com/api/oauth2/authorize")
        st.session_state.oauth_state = state
        
        st.title("🏥 MedBot ERP System")
        st.info("Будь ласка, авторизуйтесь для доступу до системи.")
        st.link_button("🔑 УВІЙТИ ЧЕРЕЗ DISCORD", auth_url, type="primary")
        
        # Кнопка для повного скидання, якщо щось застрягло
        if st.button("🔄 Очистити сесію (якщо виникає помилка)"):
            st.query_params.update(reset="1")
            st.rerun()
        st.stop()

    # 4. Обробка повернення від Discord
    if code:
        try:
            discord = OAuth2Session(
                st.secrets["DISCORD_CLIENT_ID"], 
                redirect_uri=st.secrets["DISCORD_REDIRECT_URI"], 
                state=st.session_state.get('oauth_state')
            )
            token = discord.fetch_token(
                "https://discord.com/api/oauth2/token", 
                client_secret=st.secrets["DISCORD_CLIENT_SECRET"], 
                code=code
            )
            
            # Отримуємо дані про членство на сервері
            m_res = discord.get(f"https://discord.com/api/users/@me/guilds/{st.secrets['GUILD_ID']}/member").json()
            
            # Перевірка на помилку від API Discord
            if "user" not in m_res:
                st.error("Помилка: Ви не є учасником потрібного Discord сервера.")
                if st.button("Спробувати знову"):
                    st.query_params.update(reset="1")
                    st.rerun()
                st.stop()

            u_id = m_res['user']['id']
            
            # Перевірка бану
            ban_reason = is_blacklisted(u_id)
            if ban_reason: 
                st.error(f"🚫 Доступ заблоковано: {ban_reason}")
                st.stop()

            # Перевірка ролей
            u_roles = m_res.get('roles', [])
            is_adm = st.secrets['ADMIN_ROLE_ID'] in u_roles
            is_allowed = st.secrets['ALLOWED_ROLE_ID'] in u_roles or is_adm or is_whitelisted(u_id)

            if is_allowed:
                server_nick = m_res.get('nick') or m_res['user'].get('username')
                st.session_state.auth_user = {"id": u_id, "username": server_nick, "is_admin": is_adm}
                
                # Оновлюємо сесію в БД
                expiry = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute("REPLACE INTO sessions VALUES (?, ?, ?, ?, ?)", 
                               (u_id, server_nick, json.dumps(token), expiry, 1 if is_adm else 0))
                conn.commit()
                
                # Очищаємо URL від коду Discord
                st.query_params.clear()
                st.rerun()
            else:
                st.error("У вас немає відповідної ролі в Discord.")
                st.stop()
                
        except Exception as e:
            st.error(f"Критична помилка авторизації. Можливо, посилання застаріло.")
            if st.button("🔄 Скинути і почати заново"):
                st.query_params.update(reset="1")
                st.rerun()
            st.stop()

handle_discord_login()
user = st.session_state.auth_user
current_coords = load_user_coords(user['id'])

# --- ОСНОВНИЙ ІНТЕРФЕЙС ---
st.sidebar.title(f"👤 {user['username']}")
if user['is_admin']: st.sidebar.subheader("👑 Адміністратор")
else: st.sidebar.caption("🩺 Співробітник")

menu = st.sidebar.radio("Навігація", ["📄 Сканер", "⚙️ Налаштування", "📊 Адмін-панель", "🚪 Вихід"])

if menu == "🚪 Вихід":
    cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user['id'],))
    conn.commit(); st.session_state.auth_user = None; st.rerun()

elif menu == "📊 Адмін-панель":
    if not user.get('is_admin'):
        st.error("У вас немає прав доступу до цієї панелі.")
    else:
        t_logs, t_ban, t_white, t_users = st.tabs(["📝 Логи", "🚫 Бан-лист", "💎 VIP", "👥 Активні сесії"])
        
        with t_logs:
            h = cursor.execute("SELECT user_name, count, timestamp FROM logs ORDER BY timestamp DESC LIMIT 50").fetchall()
            st.table([{"Користувач": r[0], "К-сть": r[1], "Дата": r[2]} for r in h])
        
        with t_ban:
            c1, c2 = st.columns(2)
            bid = c1.text_input("Discord ID для бану")
            breason = c2.text_input("Причина бану")
            if st.button("🚫 Забанити"):
                if bid:
                    cursor.execute("REPLACE INTO blacklist VALUES (?, 'Banned', ?)", (bid, breason))
                    cursor.execute("DELETE FROM sessions WHERE user_id = ?", (bid,))
                    conn.commit(); st.rerun()
            for b in cursor.execute("SELECT user_id, reason FROM blacklist").fetchall():
                col_a, col_b = st.columns([3, 1])
                col_a.write(f"🆔 `{b[0]}` | Причина: {b[1]}")
                if col_b.button("Розбан", key=f"un_{b[0]}"):
                    cursor.execute("DELETE FROM blacklist WHERE user_id = ?", (b[0],)); conn.commit(); st.rerun()

        with t_white:
            c1, c2 = st.columns(2)
            wid = c1.text_input("Discord ID для VIP")
            wname = c2.text_input("Примітка для VIP")
            if st.button("➕ Додати"):
                cursor.execute("REPLACE INTO whitelist VALUES (?, ?)", (wid, wname))
                conn.commit(); st.rerun()
            for w in cursor.execute("SELECT user_id, user_name FROM whitelist").fetchall():
                col_a, col_b = st.columns([3, 1])
                col_a.write(f"💎 `{w[0]}` | {w[1]}")
                if col_b.button("Видалити", key=f"dw_{w[0]}"):
                    cursor.execute("DELETE FROM whitelist WHERE user_id = ?", (w[0],)); conn.commit(); st.rerun()

        with t_users:
            st.write("### Всі авторизовані користувачі")
            for r in cursor.execute("SELECT user_id, user_name, expiry, is_admin FROM sessions").fetchall():
                col_a, col_b = st.columns([3, 1])
                col_a.code(f"{'👑' if r[3] else '🩺'} {r[1]} | ID: {r[0]} | До: {r[2]}")
                if col_b.button("Видалити", key=f"ks_{r[0]}"):
                    cursor.execute("DELETE FROM sessions WHERE user_id = ?", (r[0],)); conn.commit(); st.rerun()

elif menu == "⚙️ Налаштування":
    st.header("📐 Налаштування трафарету")
    
    # Виводимо статус кожної зони
    c1, c2, c3 = st.columns(3)
    c1.write(f"Прізвище: {'✅' if current_coords.get('Surname') else '❌'}")
    c2.write(f"Ім'я: {'✅' if current_coords.get('Name') else '❌'}")
    c3.write(f"ID: {'✅' if current_coords.get('ID') else '❌'}")
    
    if st.button("🗑 Скинути всі налаштування"):
        save_user_coords(user['id'], {"Surname": None, "Name": None, "ID": None})
        st.rerun()
    
    f = st.file_uploader("Завантажте зразок фото", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Оберіть зону для виділення", ["Surname", "Name", "ID"])
        
        # Відображення кроппера
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        
        if st.button(f"💾 Зберегти зону {target}"):
            # Створюємо чистий словник для запису
            updated_coords = {
                "Surname": current_coords.get("Surname"),
                "Name": current_coords.get("Name"),
                "ID": current_coords.get("ID")
            }
            
            # Записуємо нові координати як цілі числа
            updated_coords[target] = (
                int(rect['left']), 
                int(rect['top']), 
                int(rect['width']), 
                int(rect['height'])
            )
            
            save_user_coords(user['id'], updated_coords)
            st.success(f"Зона {target} успішно оновлена!")
            time.sleep(1)
            st.rerun()

elif menu == "📄 Сканер":
    if not all(current_coords.values()): 
        st.error("Налаштуйте трафарет!")
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
            if st.button("🚀 ВІДПРАВИТИ ЗВІТ"):
                if not c_files: st.error("Додайте докази.")
                else:
                    # Твій новий формат
                    user_mention = f"<@{user['id']}>"
                    server_nick = user['username']
                    user_info_report = f"{user_mention} | {server_nick}"
                    
                    msg = f"🏥 **ЗВІТ**\n**Співробітник:** {user_info_report}\n**К-сть:** {len(final)}\n\n" + \
                          "\n".join([f"• {r['Surname']} {r['Name']} [ID: {r['ID']}]" for r in final])
                    
                    try:
                        requests.post(st.secrets["DISCORD_WEBHOOK_URL"], data={"content": msg}, files=st.session_state.passport_payload)
                        c_pay = [(f"c{i}", (f"c_{i}.jpg", compress_image(cf).read(), "image/jpeg")) for i, cf in enumerate(c_files)]
                        requests.post(st.secrets["DISCORD_WEBHOOK_URL"], data={"content": "💳 **Докази:**"}, files=c_pay)
                        cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (user['id'], user['username'], len(final), datetime.now().strftime("%Y-%m-%d %H:%M")))
                        conn.commit(); st.success("Надіслано!"); st.session_state.scanned_data = []; time.sleep(2); st.rerun()
                    except Exception as e: st.error(f"Помилка: {e}")



