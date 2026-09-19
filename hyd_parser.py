"""
hyd_parser.py
Parser สำหรับไฟล์ 'Rainfall-Runoff-YYYY.xlsx' ที่มีชีตชื่อ HYD1, HYD2, ... รวมข้อมูลหลายสถานีในไฟล์เดียว
โครงสร้างแต่ละชีต: แถวหัวมีชื่อสถานี (เช่น 'N.1') อยู่ที่คอลัมน์ 'ระดับ' ของสถานีนั้น
คอลัมน์ถัดไปคือ 'ปริมาณ' (ปริมาณน้ำท่า, ลบ.ม./วิ) ของสถานีเดียวกัน
แถวที่มีคำว่า 'วันที่' ในคอลัมน์แรก คือแถวก่อนข้อมูลจริงจะเริ่ม (ข้อมูลเป็นรายวัน คอลัมน์แรก = วันที่)
"""
import io
import datetime
import openpyxl


def parse_hyd_workbook(file_bytes: bytes, station_label_map: dict) -> dict:
    """station_label_map: {internal_code: label_in_file} เช่น {'N1': 'N.1', 'N13A': 'N.13A', 'N64': 'N.64'}
    คืนค่า dict {internal_code: {date: ปริมาณน้ำ (ลบ.ม./วิ)}}
    ค้นหาทุกชีตในไฟล์ (HYD1, HYD2, ...) เผื่อสถานีที่ต้องการอยู่คนละชีตกัน"""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    results = {code: {} for code in station_label_map}
    labels = list(station_label_map.values())

    for sheet in wb.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue

        # หาแถวหัวที่มีชื่อสถานีอย่างน้อย 1 ตัวที่ต้องการ (ค้นในแถวแรกๆ ของชีต)
        header_row_idx = None
        for i in range(min(10, len(rows))):
            if rows[i] and any(c in labels for c in rows[i]):
                header_row_idx = i
                break
        if header_row_idx is None:
            continue
        header_row = rows[header_row_idx]

        # หาแถวที่คอลัมน์แรกเขียนว่า 'วันที่' — แถวถัดไปคือจุดเริ่มข้อมูลจริง
        data_start_idx = None
        for i in range(header_row_idx, min(header_row_idx + 15, len(rows))):
            if rows[i] and rows[i][0] == "วันที่":
                data_start_idx = i + 1
                break
        if data_start_idx is None:
            continue

        for code, label in station_label_map.items():
            if label not in header_row:
                continue
            level_col = header_row.index(label)
            q_col = level_col + 1  # คอลัมน์ถัดไปคือ 'ปริมาณ' เสมอตามโครงสร้างไฟล์นี้
            for r in rows[data_start_idx:]:
                if not r:
                    continue
                dt = r[0]
                if not isinstance(dt, (datetime.datetime, datetime.date)):
                    continue
                d = dt.date() if isinstance(dt, datetime.datetime) else dt
                if q_col < len(r) and r[q_col] is not None:
                    try:
                        results[code][d] = float(r[q_col])
                    except (TypeError, ValueError):
                        continue

    return results
