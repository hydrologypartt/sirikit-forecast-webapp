"""
data_io.py
จัดการอ่าน/เขียนข้อมูลของแอป — รองรับ 2 โหมด:
  - โหมด local: เก็บไฟล์ไว้ในโฟลเดอร์ ./data (ใช้ตอนพัฒนา/ทดสอบในเครื่อง)
  - โหมด drive: เก็บไฟล์ไว้ใน Google Drive ผ่าน Service Account (ใช้ตอน deploy จริง)

แนวคิดการเก็บข้อมูล: แยกไฟล์ snapshot รายวัน (เช่น reservoir_2026-09-08.csv)
แทนการเขียนทับไฟล์เดียว เพื่อไม่ให้บั๊กวันเดียวกระทบข้อมูลวันอื่น (ตามที่ตกลงกันไว้)
"""
import os
import io
import glob
import datetime
import pandas as pd
import streamlit as st

LOCAL_DATA_DIR = "data/snapshots"
LOCAL_STATION_DIR = "data/stations"
LOCAL_MODEL_DIR = "data/models"
DRIVE_FOLDER_ID_KEY = "gdrive_folder_id"  # เก็บใน st.secrets


def storage_mode():
    """คืนค่า 'drive' ถ้ามีการตั้งค่า Google service account ไว้ใน st.secrets, ไม่งั้นใช้ 'local'
    ครอบ try/except ไว้เพราะถ้าไม่มีไฟล์ secrets.toml เลย (ปกติตอนทดสอบในเครื่อง)
    Streamlit บางเวอร์ชันจะ raise error แทนที่จะคืนค่าว่าง"""
    try:
        return "drive" if "gcp_service_account" in st.secrets else "local"
    except Exception:
        return "local"


# ---------------------------------------------------------------------------
# Google Drive helpers
# ---------------------------------------------------------------------------
@st.cache_resource
def _get_drive_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)


def _drive_folder_id():
    return st.secrets[DRIVE_FOLDER_ID_KEY]


def _drive_upload(filename, content_bytes, mimetype="text/csv"):
    from googleapiclient.http import MediaIoBaseUpload

    service = _get_drive_service()
    folder_id = _drive_folder_id()
    # ถ้ามีไฟล์ชื่อนี้อยู่แล้วให้อัปเดตแทนสร้างใหม่ซ้ำ
    existing = service.files().list(
        q=f"name='{filename}' and '{folder_id}' in parents and trashed=false",
        fields="files(id)",
    ).execute().get("files", [])
    media = MediaIoBaseUpload(io.BytesIO(content_bytes), mimetype=mimetype, resumable=False)
    if existing:
        service.files().update(fileId=existing[0]["id"], media_body=media).execute()
    else:
        service.files().create(
            body={"name": filename, "parents": [folder_id]}, media_body=media
        ).execute()


def _drive_list_filenames(prefix=""):
    service = _get_drive_service()
    folder_id = _drive_folder_id()
    q = f"'{folder_id}' in parents and trashed=false"
    files = service.files().list(q=q, fields="files(id,name)", pageSize=1000).execute().get("files", [])
    return [f for f in files if f["name"].startswith(prefix)]


def _drive_download(file_id):
    from googleapiclient.http import MediaIoBaseDownload

    service = _get_drive_service()
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Public API — ใช้เหมือนกันไม่ว่าจะเป็น local หรือ drive
# ---------------------------------------------------------------------------
def save_reservoir_snapshot(df_new_rows: pd.DataFrame):
    """บันทึกแถวข้อมูลใหม่ (1 หรือหลายวัน) เป็นไฟล์ snapshot แยกรายวัน
    df_new_rows ต้องมีคอลัมน์: date, capacity, usable, level, storage, pct, inflow, release"""
    for _, row in df_new_rows.iterrows():
        date_str = pd.to_datetime(row["date"]).strftime("%Y-%m-%d")
        filename = f"reservoir_{date_str}.csv"
        content = row.to_frame().T.to_csv(index=False).encode("utf-8")
        if storage_mode() == "drive":
            _drive_upload(filename, content)
        else:
            os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
            with open(os.path.join(LOCAL_DATA_DIR, filename), "wb") as f:
                f.write(content)


@st.cache_data(ttl=300)
def load_all_reservoir_snapshots() -> pd.DataFrame:
    """โหลด snapshot ทั้งหมด รวมเป็น DataFrame เดียว เรียงตามวันที่"""
    frames = []
    if storage_mode() == "drive":
        for f in _drive_list_filenames(prefix="reservoir_"):
            content = _drive_download(f["id"])
            frames.append(pd.read_csv(io.BytesIO(content)))
    else:
        os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
        for path in glob.glob(os.path.join(LOCAL_DATA_DIR, "reservoir_*.csv")):
            frames.append(pd.read_csv(path))
    if not frames:
        return pd.DataFrame(
            columns=["date", "capacity", "usable", "level", "storage", "pct", "inflow", "release"]
        )
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset="date", keep="last").sort_values("date").reset_index(drop=True)
    return df


def save_station_file(station_name: str, content_bytes: bytes, ext: str = "txt"):
    """บันทึกไฟล์สถานีน้ำท่าดิบ (เก็บฉบับล่าสุดที่อัปโหลดไว้ทั้งไฟล์ ไม่ต้อง snapshot รายวัน) — ใช้ตอนนำเข้าข้อมูลย้อนหลังครั้งแรก
    รองรับทั้ง .txt (รูปแบบรายงาน RID) และ .xlsx (ตารางวันที่+Q อย่างง่าย)"""
    ext = ext.lower().lstrip(".")
    filename = f"station_{station_name}.{ext}"
    mimetype = "text/plain" if ext == "txt" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if storage_mode() == "drive":
        # ลบไฟล์นามสกุลอื่นของสถานีเดิมทิ้งก่อน กันมีทั้ง .txt และ .xlsx ค้างพร้อมกัน
        for other_ext in ("txt", "xlsx"):
            if other_ext != ext:
                for f in _drive_list_filenames(prefix=f"station_{station_name}.{other_ext}"):
                    _get_drive_service().files().delete(fileId=f["id"]).execute()
        _drive_upload(filename, content_bytes, mimetype=mimetype)
    else:
        os.makedirs(LOCAL_STATION_DIR, exist_ok=True)
        for other_ext in ("txt", "xlsx"):
            if other_ext != ext:
                other_path = os.path.join(LOCAL_STATION_DIR, f"station_{station_name}.{other_ext}")
                if os.path.exists(other_path):
                    os.remove(other_path)
        with open(os.path.join(LOCAL_STATION_DIR, filename), "wb") as f:
            f.write(content_bytes)


def load_station_file_bytes(station_name: str):
    """คืนค่า (content_bytes, ext) ของไฟล์สถานีที่นำเข้าไว้ล่าสุด (.txt หรือ .xlsx) หรือ (None, None) ถ้าไม่มี"""
    for ext in ("txt", "xlsx"):
        filename = f"station_{station_name}.{ext}"
        if storage_mode() == "drive":
            matches = [f for f in _drive_list_filenames(prefix=filename) if f["name"] == filename]
            if matches:
                return _drive_download(matches[0]["id"]), ext
        else:
            path = os.path.join(LOCAL_STATION_DIR, filename)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    return f.read(), ext
    return None, None


def save_hyd_workbook(file_bytes: bytes):
    """เก็บไฟล์ 'Rainfall-Runoff-YYYY.xlsx' (รวมหลายสถานีในไฟล์เดียว) — เขียนทับไฟล์เดิมทุกครั้งที่อัปโหลดใหม่
    ใช้เติมช่วงที่ไฟล์ .txt รายสถานี (แบบ Water Year) ยังไม่อัปเดตถึงปัจจุบัน"""
    filename = "hyd_workbook.xlsx"
    if storage_mode() == "drive":
        _drive_upload(filename, file_bytes, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        os.makedirs(LOCAL_STATION_DIR, exist_ok=True)
        with open(os.path.join(LOCAL_STATION_DIR, filename), "wb") as f:
            f.write(file_bytes)


def load_hyd_workbook_bytes():
    filename = "hyd_workbook.xlsx"
    if storage_mode() == "drive":
        matches = [f for f in _drive_list_filenames(prefix=filename) if f["name"] == filename]
        if not matches:
            return None
        return _drive_download(matches[0]["id"])
    else:
        path = os.path.join(LOCAL_STATION_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()


def save_catchment_shp(shp_bytes: bytes):
    """เก็บไฟล์ .shp (เฉพาะไฟล์ .shp พอ ไม่ต้องมี .dbf/.shx เพราะ shp_reader อ่านแค่ .shp)
    ใช้ตัดพื้นที่รับน้ำสำหรับคำนวณฝนเฉลี่ยพื้นที่ — อัปโหลดครั้งเดียว ไม่ต้องทำซ้ำทุกวัน"""
    filename = "catchment.shp"
    if storage_mode() == "drive":
        _drive_upload(filename, shp_bytes, mimetype="application/octet-stream")
    else:
        os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
        with open(os.path.join(LOCAL_DATA_DIR, filename), "wb") as f:
            f.write(shp_bytes)


def load_catchment_shp_bytes():
    filename = "catchment.shp"
    if storage_mode() == "drive":
        matches = [f for f in _drive_list_filenames(prefix=filename) if f["name"] == filename]
        if not matches:
            return None
        return _drive_download(matches[0]["id"])
    else:
        path = os.path.join(LOCAL_DATA_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()


def save_reservoir_bulk(df: pd.DataFrame):
    """บันทึกข้อมูลอ่างย้อนหลังจำนวนมากเป็นไฟล์เดียว (แทนที่จะแยกไฟล์ต่อวันเป็นพันๆ ไฟล์)
    ใช้ตอนนำเข้าข้อมูลย้อนหลังครั้งแรกเท่านั้น — การอัปเดตรายวันปกติยังคงใช้ save_reservoir_snapshot ต่อไป"""
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"reservoir_bulk_{ts}.csv"
    df_out = df.copy()
    df_out["date"] = pd.to_datetime(df_out["date"]).dt.strftime("%Y-%m-%d")
    content = df_out.to_csv(index=False).encode("utf-8")
    if storage_mode() == "drive":
        _drive_upload(filename, content)
    else:
        os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
        with open(os.path.join(LOCAL_DATA_DIR, filename), "wb") as f:
            f.write(content)


def save_station_snapshot(df_new_rows: pd.DataFrame):
    """บันทึกข้อมูลสถานีน้ำท่าที่กรอกเองรายวัน (ระดับน้ำ + ปริมาณน้ำ ของ N1/N13A/N64)
    df_new_rows ต้องมีคอลัมน์: date, N1_level, N1_Q, N13A_level, N13A_Q, N64_level, N64_Q"""
    for _, row in df_new_rows.iterrows():
        date_str = pd.to_datetime(row["date"]).strftime("%Y-%m-%d")
        filename = f"stationQ_{date_str}.csv"
        content = row.to_frame().T.to_csv(index=False).encode("utf-8")
        if storage_mode() == "drive":
            _drive_upload(filename, content)
        else:
            os.makedirs(LOCAL_STATION_DIR, exist_ok=True)
            with open(os.path.join(LOCAL_STATION_DIR, filename), "wb") as f:
                f.write(content)


@st.cache_data(ttl=300)
def load_all_station_snapshots() -> pd.DataFrame:
    """โหลดข้อมูลสถานีน้ำท่าที่กรอกเองรายวันทั้งหมด รวมเป็น DataFrame เดียว"""
    frames = []
    cols = ["date", "N1_level", "N1_Q", "N13A_level", "N13A_Q", "N64_level", "N64_Q"]
    if storage_mode() == "drive":
        for f in _drive_list_filenames(prefix="stationQ_"):
            frames.append(pd.read_csv(io.BytesIO(_drive_download(f["id"]))))
    else:
        os.makedirs(LOCAL_STATION_DIR, exist_ok=True)
        for path in glob.glob(os.path.join(LOCAL_STATION_DIR, "stationQ_*.csv")):
            frames.append(pd.read_csv(path))
    if not frames:
        return pd.DataFrame(columns=cols)
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset="date", keep="last").sort_values("date").reset_index(drop=True)
    return df


def save_rainfall_forecast(df: pd.DataFrame):
    """เก็บฝนคาดการณ์ 7 วันข้างหน้าที่อัปโหลดล่าสุด (ไฟล์เดียว เขียนทับทุกครั้งที่อัปโหลดใหม่)
    df ต้องมีคอลัมน์: date, rainfall"""
    filename = "rainfall_forecast_pending.csv"
    df_out = df.copy()
    df_out["date"] = pd.to_datetime(df_out["date"]).dt.strftime("%Y-%m-%d")
    content = df_out.to_csv(index=False).encode("utf-8")
    if storage_mode() == "drive":
        _drive_upload(filename, content)
    else:
        os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
        with open(os.path.join(LOCAL_DATA_DIR, filename), "wb") as f:
            f.write(content)


@st.cache_data(ttl=60)
def load_rainfall_forecast() -> pd.DataFrame:
    """โหลดฝนคาดการณ์ที่อัปโหลดไว้ล่าสุด — คืนค่า DataFrame ว่างถ้ายังไม่เคยอัปโหลด"""
    filename = "rainfall_forecast_pending.csv"
    if storage_mode() == "drive":
        matches = [f for f in _drive_list_filenames(prefix=filename) if f["name"] == filename]
        if not matches:
            return pd.DataFrame(columns=["date", "rainfall"])
        content = _drive_download(matches[0]["id"])
    else:
        path = os.path.join(LOCAL_DATA_DIR, filename)
        if not os.path.exists(path):
            return pd.DataFrame(columns=["date", "rainfall"])
        with open(path, "rb") as f:
            content = f.read()
    df = pd.read_csv(io.BytesIO(content))
    df["date"] = pd.to_datetime(df["date"])
    return df


def save_rainfall_snapshot(df_new_rows: pd.DataFrame):
    """บันทึกข้อมูลฝนเฉลี่ยพื้นที่รายวัน (สำหรับสะสมไว้เทรนโมเดล)
    df_new_rows ต้องมีคอลัมน์: date, rainfall (มม.)"""
    for _, row in df_new_rows.iterrows():
        date_str = pd.to_datetime(row["date"]).strftime("%Y-%m-%d")
        filename = f"rainfall_{date_str}.csv"
        content = row.to_frame().T.to_csv(index=False).encode("utf-8")
        if storage_mode() == "drive":
            _drive_upload(filename, content)
        else:
            os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
            with open(os.path.join(LOCAL_DATA_DIR, filename), "wb") as f:
                f.write(content)


@st.cache_data(ttl=300)
def load_all_rainfall_snapshots() -> pd.DataFrame:
    frames = []
    if storage_mode() == "drive":
        for f in _drive_list_filenames(prefix="rainfall_"):
            frames.append(pd.read_csv(io.BytesIO(_drive_download(f["id"]))))
    else:
        os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
        for path in glob.glob(os.path.join(LOCAL_DATA_DIR, "rainfall_*.csv")):
            frames.append(pd.read_csv(path))
    if not frames:
        return pd.DataFrame(columns=["date", "rainfall"])
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset="date", keep="last").sort_values("date").reset_index(drop=True)
    return df


def save_forecast_log(run_date: datetime.date, forecast_values: list):
    """เก็บ log ว่าวันไหนพยากรณ์ 7 วันข้างหน้าไว้เท่าไหร่บ้าง — ไว้ย้อนดูภายหลังว่าพยากรณ์แม่นแค่ไหน"""
    date_str = run_date.strftime("%Y-%m-%d")
    filename = f"forecastlog_{date_str}.csv"
    row = {"run_date": date_str}
    for i, v in enumerate(forecast_values, start=1):
        row[f"t+{i}"] = v
    content = pd.DataFrame([row]).to_csv(index=False).encode("utf-8")
    if storage_mode() == "drive":
        _drive_upload(filename, content)
    else:
        os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
        with open(os.path.join(LOCAL_DATA_DIR, filename), "wb") as f:
            f.write(content)


def save_models(model_bytes: bytes):
    """บันทึกไฟล์โมเดลที่เทรนแล้ว (joblib serialize มาเป็น bytes ก่อนเรียกฟังก์ชันนี้)"""
    filename = "models.joblib"
    if storage_mode() == "drive":
        _drive_upload(filename, model_bytes, mimetype="application/octet-stream")
    else:
        os.makedirs(LOCAL_MODEL_DIR, exist_ok=True)
        with open(os.path.join(LOCAL_MODEL_DIR, filename), "wb") as f:
            f.write(model_bytes)


def load_models_bytes():
    filename = "models.joblib"
    if storage_mode() == "drive":
        matches = [f for f in _drive_list_filenames(prefix=filename) if f["name"] == filename]
        if not matches:
            return None
        return _drive_download(matches[0]["id"])
    else:
        path = os.path.join(LOCAL_MODEL_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()


@st.cache_data(ttl=300)
def load_all_forecast_logs() -> pd.DataFrame:
    frames = []
    if storage_mode() == "drive":
        for f in _drive_list_filenames(prefix="forecastlog_"):
            frames.append(pd.read_csv(io.BytesIO(_drive_download(f["id"]))))
    else:
        os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
        for path in glob.glob(os.path.join(LOCAL_DATA_DIR, "forecastlog_*.csv")):
            frames.append(pd.read_csv(path))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values("run_date").reset_index(drop=True)
