"""
Parser สำหรับไฟล์รายงาน 'Daily Mean Discharge' ของกรมชลประทาน (RID Hydrology Division)
รูปแบบตาราง Water Year (เม.ย. - มี.ค. ของปีถัดไป) แบบ fixed-width
"""
import re
import datetime

MONTHS_ORDER = ["Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar"]
MONTH_NUM = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def parse_station_file(path_or_bytes):
    """คืนค่า dict {date: discharge_cms}"""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        text = path_or_bytes.decode("utf-8", errors="replace")
        lines = text.splitlines()
    else:
        with open(path_or_bytes, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

    records = {}
    col_bounds = None
    water_year_start = None

    for line in lines:
        line = line.rstrip("\n").rstrip("\r")

        wy_match = re.match(r"\s*Water Year - (\d+)", line)
        if wy_match:
            water_year_start = int(wy_match.group(1))
            continue

        header_match = re.search(
            r"Date\s+Apr\s+May\s+Jun\s+Jul\s+Aug\s+Sep\s+Oct\s+Nov\s+Dec\s+Jan\s+Feb\s+Mar", line
        )
        if header_match:
            labels = ["Date"] + MONTHS_ORDER
            bounds, pos = [], 0
            for lab in labels:
                idx = line.index(lab, pos)
                end = idx + len(lab)
                bounds.append(end)
                pos = end
            col_bounds = bounds
            continue

        if col_bounds and water_year_start is not None:
            m = re.match(r"^\s*(\d{1,2})\s", line)
            if m and line.strip():
                day = int(m.group(1))
                if not (1 <= day <= 31):
                    continue
                for i, month in enumerate(MONTHS_ORDER):
                    s, e = col_bounds[i], col_bounds[i + 1]
                    field = line[s:e].strip().replace(",", "")
                    if not field:
                        continue
                    try:
                        val = float(field)
                    except ValueError:
                        continue
                    year = water_year_start if month != "Jan" and month != "Feb" and month != "Mar" else water_year_start + 1
                    try:
                        dt = datetime.date(year, MONTH_NUM[month], day)
                    except ValueError:
                        continue
                    records[dt] = val
    return records


def cms_to_mcm_per_day(value_cms):
    """แปลงหน่วย m3/s -> ล้าน ลบ.ม./วัน ให้สเกลตรงกับข้อมูล inflow ของเขื่อน"""
    return value_cms * 86400 / 1_000_000


def parse_station_excel(file_bytes: bytes):
    """สำหรับไฟล์ .xlsx ของสถานีน้ำท่าที่เป็นตารางง่ายๆ 2-3 คอลัมน์:
    วันที่ (หรือ Date), ระดับน้ำ (ไม่บังคับ), ปริมาณน้ำ/Q (ลบ.ม./วิ)
    คืนค่า dict {date: discharge_cms} (เอาเฉพาะคอลัมน์ Q ไปใช้ต่อในโมเดล)
    """
    import io as _io
    import openpyxl as _openpyxl

    wb = _openpyxl.load_workbook(_io.BytesIO(file_bytes), data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("ไฟล์ว่างเปล่า")

    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    date_idx = next((i for i, h in enumerate(header) if "วันที่" in h or h.lower() == "date"), None)
    q_idx = next(
        (i for i, h in enumerate(header) if "ปริมาณน้ำ" in h or h.upper().strip() == "Q" or "discharge" in h.lower()),
        None,
    )
    if date_idx is None or q_idx is None:
        raise ValueError(
            "หาคอลัมน์ 'วันที่' และ 'ปริมาณน้ำ/Q' ไม่เจอ — ตั้งชื่อหัวคอลัมน์ในไฟล์ให้มีคำว่า 'วันที่' และ 'ปริมาณน้ำ' หรือ 'Q'"
        )

    records = {}
    for r in rows[1:]:
        raw_date, raw_q = r[date_idx], r[q_idx]
        if raw_date is None or raw_q is None:
            continue
        if isinstance(raw_date, datetime.datetime):
            dt = raw_date.date()
        elif isinstance(raw_date, datetime.date):
            dt = raw_date
        else:
            continue  # ไม่รองรับวันที่แบบข้อความไทยในไฟล์สถานี ให้ใช้ .txt แทนถ้าเป็นแบบนั้น
        try:
            records[dt] = float(raw_q)
        except (TypeError, ValueError):
            continue
    if not records:
        raise ValueError("อ่านไฟล์ไม่พบข้อมูลที่ใช้ได้เลย — เช็ครูปแบบวันที่และคอลัมน์ Q ในไฟล์")
    return records
