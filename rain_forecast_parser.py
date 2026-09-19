"""
rain_forecast_parser.py
Parser สำหรับไฟล์ .xlsx ฝนคาดการณ์ 7 วันข้างหน้า — ตารางง่ายๆ 2 คอลัมน์:
วันที่ (หรือ Date) และ ปริมาณฝนคาดการณ์ (หรือคำที่มีคำว่า 'ฝน'/'rain')
"""
import io
import datetime
import openpyxl


def parse_rainfall_forecast_excel(file_bytes: bytes) -> dict:
    """คืนค่า dict {date: ปริมาณฝน (มม.)}"""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("ไฟล์ว่างเปล่า")

    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    date_idx = next((i for i, h in enumerate(header) if "วันที่" in h or h.lower() == "date"), None)
    rain_idx = next(
        (i for i, h in enumerate(header) if "ฝน" in h or "rain" in h.lower()), None
    )
    if date_idx is None or rain_idx is None:
        raise ValueError(
            "หาคอลัมน์ 'วันที่' และ 'ฝน' ไม่เจอ — ตั้งชื่อหัวคอลัมน์ในไฟล์ให้มีคำว่า 'วันที่' และ 'ฝน' (เช่น 'ปริมาณฝนคาดการณ์ (มม.)')"
        )

    records = {}
    for r in rows[1:]:
        raw_date, raw_rain = r[date_idx], r[rain_idx]
        if raw_date is None or raw_rain is None:
            continue
        if isinstance(raw_date, datetime.datetime):
            dt = raw_date.date()
        elif isinstance(raw_date, datetime.date):
            dt = raw_date
        else:
            continue
        try:
            records[dt] = float(raw_rain)
        except (TypeError, ValueError):
            continue
    if not records:
        raise ValueError("อ่านไฟล์ไม่พบข้อมูลที่ใช้ได้เลย — เช็ครูปแบบวันที่และคอลัมน์ฝนในไฟล์")
    return records
