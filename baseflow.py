"""
baseflow.py
ปรับน้ำไหลลงอ่างที่โมเดลพยากรณ์ได้ ด้วยค่า Baseflow ที่ผู้ดูแลกำหนดรายวัน

แนวคิด:
  - ค่าปรับเก็บเป็น "ลบ.ม./วิ (cms)" ต่อวันที่ (วันที่จริง ไม่ใช่ลำดับวันที่ 1-7)
    แล้วแปลงเป็น ล้าน ลบ.ม./วัน ตอนนำไปบวกกับผลโมเดล เพื่อให้สเกลตรงกับ inflow ของเขื่อน
  - ผลที่ได้ = ผลโมเดล + ค่าปรับ (ไม่ต่ำกว่า 0)
  - ผู้ใช้ทั่วไปเห็นผลที่ปรับแล้วในหน้าพยากรณ์/กราฟสรุป แต่แก้ค่าไม่ได้
    การแก้ไขทำได้เฉพาะแท็บหลังบ้านที่ต้องล็อกอิน (บัญชีตั้งใน st.secrets)

ตั้งบัญชีใน Secrets (ดู README):
    [users]          # ผู้ใช้ทั่วไป: รันโมเดลได้
    nong = "sha256:<hex ของรหัสผ่าน>"
    [admin_users]    # ผู้ดูแล: รันโมเดล + ปรับ Baseflow
    somchai = "sha256:<hex ของรหัสผ่าน>"   # หรือใส่รหัสผ่านตรงๆ ก็ได้
"""
import hashlib
import hmac

import streamlit as st

import data_io
from station_parser import cms_to_mcm_per_day

MAX_LOGIN_ATTEMPTS = 5  # ต่อ session — กันกดเดารหัสแบบรัวๆ ในหน้าเดียว (ไม่ใช่การป้องกันระดับเซิร์ฟเวอร์)


# ---------------------------------------------------------------------------
# ล็อกอิน (2 บทบาท)
#   user  = ผู้ใช้ทั่วไป  : รันโมเดล/ดูกราฟสรุปได้
#   admin = ผู้ดูแล       : ทำได้ทุกอย่างของ user + ปรับ Baseflow ในหน้าหลังบ้าน
# ---------------------------------------------------------------------------
def _read_accounts(section: str) -> dict:
    try:
        if section in st.secrets:
            return {str(k): str(v) for k, v in dict(st.secrets[section]).items()}
    except Exception:
        pass
    return {}


def _all_accounts() -> dict:
    """{username: (stored_password, role)} — ถ้าชื่อซ้ำกันในสองหมวด ให้สิทธิ์ admin ก่อน"""
    accounts = {k: (v, "user") for k, v in _read_accounts("users").items()}
    accounts.update({k: (v, "admin") for k, v in _read_accounts("admin_users").items()})
    return accounts


def accounts_configured() -> bool:
    return len(_all_accounts()) > 0


def _password_matches(stored: str, given: str) -> bool:
    if stored.startswith("sha256:"):
        given_hash = hashlib.sha256(given.encode("utf-8")).hexdigest()
        return hmac.compare_digest(stored[len("sha256:"):].lower().encode(), given_hash.encode())
    return hmac.compare_digest(stored.encode("utf-8"), given.encode("utf-8"))


def _verify(username: str, password: str):
    """คืน role ('user'/'admin') ถ้ารหัสถูก ไม่งั้นคืน None"""
    entry = _all_accounts().get(username)
    if entry is None:
        # เทียบกับค่าหลอกเพื่อให้เวลาตอบใกล้เคียงกัน ไม่เผยว่าชื่อผู้ใช้มีอยู่จริงหรือไม่
        _password_matches("x" * 8, password)
        return None
    stored, role = entry
    return role if _password_matches(stored, password) else None


def current_user():
    return st.session_state.get("auth_user")


def current_role():
    return st.session_state.get("auth_role")


def is_logged_in() -> bool:
    return current_user() is not None


def is_admin() -> bool:
    return current_role() == "admin"


def current_admin():
    """คืนชื่อผู้ใช้ถ้าล็อกอินเป็น admin ไม่งั้น None"""
    return current_user() if is_admin() else None


def login(username: str, password: str) -> bool:
    """คืน True ถ้าล็อกอินสำเร็จ (และจำไว้ใน session)"""
    if login_locked():
        return False
    username = username.strip()
    role = _verify(username, password)
    if role is not None:
        st.session_state["auth_user"] = username
        st.session_state["auth_role"] = role
        st.session_state["auth_login_fail"] = 0
        return True
    st.session_state["auth_login_fail"] = st.session_state.get("auth_login_fail", 0) + 1
    return False


def logout():
    st.session_state.pop("auth_user", None)
    st.session_state.pop("auth_role", None)


def login_locked() -> bool:
    return st.session_state.get("auth_login_fail", 0) >= MAX_LOGIN_ATTEMPTS


# ---------------------------------------------------------------------------
# นำค่าปรับไปใช้กับผลพยากรณ์
# ---------------------------------------------------------------------------
def load_adjustment_map() -> dict:
    """คืน dict {date: cms} ของค่าปรับที่บันทึกไว้"""
    df = data_io.load_baseflow_adjustments()
    if df.empty:
        return {}
    return {d.date(): float(v) for d, v in zip(df["date"], df["adj_cms"])}


def apply_adjustment(preds, forecast_dates, adj_map=None):
    """คืน (preds_adjusted, adj_mcm_list)
    preds: ผลโมเดล (ล้าน ลบ.ม./วัน) ยาวเท่ากับ forecast_dates
    ผลที่ปรับแล้วถูกตัดไม่ให้ต่ำกว่า 0"""
    if adj_map is None:
        adj_map = load_adjustment_map()
    adj_mcm = [cms_to_mcm_per_day(adj_map.get(d.date(), 0.0)) for d in forecast_dates]
    adjusted = [max(0.0, float(p) + a) for p, a in zip(preds, adj_mcm)]
    return adjusted, adj_mcm


def has_adjustment(adj_mcm) -> bool:
    return any(abs(a) > 1e-9 for a in adj_mcm)
