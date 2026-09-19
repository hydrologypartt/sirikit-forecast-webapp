"""
reservoir_parser.py
Parser สำหรับไฟล์ 'ตารางสรุปสภาพน้ำในเขื่อน...xlsx' ของกรมชลประทาน
รูปแบบ: แถว 1-3 เป็นหัวตาราง, แถวสุดท้ายมักมี 'สูงสุด'/'ต่ำสุด' (ตัดทิ้ง)
คอลัมน์: ลำดับ, วันที่ (ไทย), ภาค, ความจุ, ปริมาณน้ำใช้การ, ระดับน้ำ, ปริมาณน้ำในอ่าง, % รนก., ไหลลงอ่าง, ระบาย
"""
import io
import datetime
import openpyxl
import pandas as pd

THAI_MONTHS = {
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4,
    "พฤษภาคม": 5, "มิถุนายน": 6, "กรกฎาคม": 7, "สิงหาคม": 8,
    "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
}


def _parse_thai_date(s):
    if not isinstance(s, str):
        return None
    parts = s.split()
    if len(parts) != 3:
        return None
    try:
        d = int(parts[0])
        m = THAI_MONTHS[parts[1]]
        y = int(parts[2]) - 543  # พ.ศ. -> ค.ศ.
        return datetime.date(y, m, d)
    except Exception:
        return None


def parse_reservoir_excel(file_bytes: bytes) -> pd.DataFrame:
    """คืนค่า DataFrame คอลัมน์: date, capacity, usable, level, storage, pct, inflow, release"""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(min_row=4, values_only=True))  # ข้าม 3 แถวหัวตาราง

    records = []
    for r in rows:
        dt = _parse_thai_date(r[1])
        if dt is None:
            continue  # ข้ามแถวสรุป (สูงสุด/ต่ำสุด) หรือแถวว่าง
        records.append(
            {
                "date": dt,
                "capacity": r[3],
                "usable": r[4],
                "level": r[5],
                "storage": r[6],
                "pct": r[7],
                "inflow": r[8],
                "release": r[9],
            }
        )
    if not records:
        raise ValueError(
            "อ่านไฟล์ไม่พบข้อมูลที่ parse ได้เลย — เช็คว่าเป็นไฟล์รูปแบบ 'ตารางสรุปสภาพน้ำในเขื่อน' ของกรมชลประทานหรือไม่"
        )
    df = pd.DataFrame(records).sort_values("date").reset_index(drop=True)
    return df
