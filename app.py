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

# --- НАЛАШТУВАННЯ СЕРВЕРА ---
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
if os.path.exists('/usr/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

st.set_page_config(layout="wide", page_title="MedBot ERP Pro")

# --- БАЗА ДАНИХ ---
conn = sqlite3.connect("medbot_db.sqlite", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, user_name TEXT, count INTEGER, timestamp TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS user_coords (user_id TEXT PRIMARY KEY, coords_json TEXT)')
conn.commit()

# --- СЕКРЕТИ ---
try:
    c = st.secrets
    conf = {
        "ID": c["DISCORD_CLIENT_ID"],
        "SEC": c["DISCORD_CLIENT_SECRET"],
        "URI": c["DISCORD_REDIRECT_URI"],
        "G_ID": c["GUILD_ID"],
        "A_ID": c["ADMIN_ROLE_ID"],
        "R_ID": c["ALLOWED_ROLE_ID"],
        "WEB": c["DISCORD_WEBHOOK_URL"]
    }
except Exception as e:
    st.error(f"Помилка Secrets: {e}")
    st.stop()

# --- ФУНКЦІЇ ---
def ocr_box(img_np, is_id=False):
    gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
    _, thr = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    txt = pytesseract.image_to_string(thr, config='--psm 7')
    if is_id: return "".join(re.findall(r'\d+', txt))
    return re.sub(r'[^a-zA-Zа-яА-ЯіїєґІЇЄҐ]', '', txt).strip().capitalize()

def img_to_bytes(img_file):
    img = Image.open(img_file).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75)
    buf.seek(0)
    return buf.read()

# --- СЕСІЯ ---
if 'user' not in st.session_state: st.session_state.user = None
if 'data' not in st.session_state: st.session_state.data = []

# --- АВТОРИЗАЦІЯ ---
def login():
    st.title("🏥 MedBot ERP")
    qp = st.query_params
    if "code" in qp:
        try:
            sess = OAuth2Session(conf["ID"], redirect_uri=conf["URI"], scope=['identify', 'guilds.members.read'])
            sess.fetch_token('https://discord.com/api/oauth2/token', client_secret=conf["SEC"], code=qp["code"])
            u = sess.get('https://discord.com/api/users/@me').json()
            m = sess.get(f"https://discord.com/api/users/@me/guilds/{conf['G_ID']}/member").json()
            roles = m.get('roles', [])
            if conf["R_ID"] in roles or conf["A_ID"] in roles:
                st.session_state.user = {"id": u['id'], "name": u['username'], "adm": conf["A_ID"] in roles}
                st.query_params.clear()
                st.rerun()
        except Exception as e: st.error(f"Auth Error: {e}")

    url = f"https://discord.com/api/oauth2/authorize?client_id={conf['ID']}&redirect_uri={requests.utils.quote(conf['URI'])}&response_type=code&scope=identify%20guilds.members.read"
    st.markdown(f'<div style="text-align:center;margin-top:50px"><a href="{url}" target="_top" style="background:#5865F2;color:white;padding:20px 40px;text-decoration:none;border-radius:10px;font-weight:bold;font-size:20px">🔑 УВІЙТИ ЧЕРЕЗ DISCORD</a></div>', unsafe_allow_html=True)

if not st.session_state.user:
    login()
    st.stop()

# --- МЕНЮ ---
u = st.session_state.user
menu = st.sidebar.radio(f"👤 {u['name']}", ["Сканер", "Налаштування", "Вихід"])

if menu == "Вихід":
    st.session_state.user = None
    st.rerun()

elif menu == "Налаштування":
    st.header("📐 Трафарет")
    f = st.file_uploader("Зразок", type=['png','jpg','jpeg'])
    if f:
        img = Image.open(f).convert("RGB").resize((1920, 1080))
        target = st.selectbox("Поле", ["Surname", "Name", "ID"])
        rect = st_cropper(img, realtime_update=True, box_color='#FF0000', return_type='box')
        if st.button("💾 Зберегти"):
            cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u['id'],))
            row = cursor.fetchone()
            coords = json.loads(row[0]) if row else {"Surname": None, "Name": None, "ID": None}
            coords[target] = (rect['left'], rect['top'], rect['width'], rect['height'])
            cursor.execute("REPLACE INTO user_coords VALUES (?, ?)", (u['id'], json.dumps(coords)))
            conn.commit()
            st.success("Збережено!")

elif menu == "Сканер":
    cursor.execute("SELECT coords_json FROM user_coords WHERE user_id = ?", (u['id'],))
    row = cursor.fetchone()
    if not row: st.warning("⚠️ Налаштуйте трафарет!")
    else:
        coords = json.loads(row[0])
        files = st.file_uploader("Паспорти", accept_multiple_files=True, type=['png','jpg','jpeg'])
        if files and st.button("🔍 OCR"):
            st.session_state.data = []
            st.session_state.payload = []
            for i, f in enumerate(files):
                img_np = np.array(Image.open(f).convert("RGB").resize((1920, 1080)))
                res = {k: ocr_box(img_np[int(v[1]):int(v[1]+v[3]), int(v[0]):int(v[0]+v[2])], k=="ID") for k, v in coords.items() if v}
                st.session_state.data.append(res)
                st.session_state.payload.append((f"p{i}", (f"p{i}.jpg", img_to_bytes(f), "image/jpeg")))
            st.rerun()

        if st.session_state.data:
            for idx, itm in enumerate(st.session_state.data):
                c1, c2, c3 = st.columns(3)
                itm['Surname'] = c1.text_input(f"Пр. #{idx+1}", itm['Surname'], key=f"s{idx}")
                itm['Name'] = c2.text_input(f"Ім. #{idx+1}", itm['Name'], key=f"n{idx}")
                itm['ID'] = c3.text_input(f"ID #{idx+1}", itm['ID'], key=f"i{idx}")
            
            cheks = st.file_uploader("Чеки", accept_multiple_files=True, type=['png','jpg','jpeg'])
            if st.button("🚀 ВІДПРАВИТИ"):
                msg = f"🏥 **ЗВІТ** від <@{u['id']}>\n" + "\n".join([f"• {r['Surname']} {r['Name']} (ID: {r['ID']})" for r in st.session_state.data])
                requests.post(conf["WEB"], data={"content": msg}, files=st.session_state.payload)
                if cheks:
                    c_files = [(f"c{i}", (f"c{i}.jpg", img_to_bytes(cf), "image/jpeg")) for i, cf in enumerate(cheks)]
                    requests.post(conf["WEB"], data={"content": "💳 Чеки:"}, files=c_files)
                cursor.execute("INSERT INTO logs VALUES (?, ?, ?, ?)", (u['id'], u['name'], len(st.session_state.data), datetime.now().strftime("%d.%m %H:%M")))
                conn.commit()
                st.success("✅ Надіслано!")
                st.session_state.data = []
                st.rerun()
