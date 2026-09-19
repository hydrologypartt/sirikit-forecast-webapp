"""
rainfall.py
ประมวลผลไฟล์ฝนพยากรณ์แบบกริด (รูปแบบ WRF, คอลัมน์: lat, lon, 00:00Z <วันที่> x N วัน)
clip เฉพาะจุดในพื้นที่รับน้ำ (shapefile) แล้วเฉลี่ยเป็นค่าเดียวต่อวัน (areal average)

กฎที่ตกลงกันไว้: วันที่ 1-3 ใช้ไฟล์ d02 (ละเอียด), วันที่ 4-7 ใช้ไฟล์ d01 (หยาบแต่ครอบคลุมไกลกว่า)
"""
import re
import datetime
import pandas as pd
from shp_reader import read_shapefile_polygons, clip_points


def _parse_date_from_header(col_name: str):
    """แปลงหัวคอลัมน์แบบ '00:00Z 2026-09-07' -> datetime.date(2026,9,7)"""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(col_name))
    if not m:
        return None
    return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _areal_mean_per_day(csv_path_or_buffer, shapes):
    df = pd.read_csv(csv_path_or_buffer)
    date_cols = [c for c in df.columns if c not in ("lat", "lon")]
    points = list(df[["lat", "lon"] + date_cols].itertuples(index=False, name=None))
    inside = clip_points(points, shapes)
    if not inside:
        raise ValueError(
            "ไม่มีจุดกริดใดอยู่ในขอบเขตพื้นที่รับน้ำเลย — เช็คว่าไฟล์ฝนกับ shapefile ครอบคลุมพื้นที่เดียวกันหรือไม่"
        )
    inside_df = pd.DataFrame(inside, columns=["lat", "lon"] + date_cols)
    means = inside_df[date_cols].mean()
    means.index = [_parse_date_from_header(c) for c in means.index]
    return means  # index = วันที่ (datetime.date), value = ฝนเฉลี่ยพื้นที่ (มม.)


def build_7day_rainfall(d02_path, d01_path, shapefile_path, bias_correct=False, horizon=7):
    """คืนค่า pandas Series ความยาว `horizon` วัน (index เป็นวันที่จริง) ของฝนเฉลี่ยพื้นที่รับน้ำ
    ใช้ d02 (ละเอียด) เท่าที่มีข้อมูลก่อน แล้วเติมวันที่เหลือด้วย d01 (หยาบแต่ครอบคลุมไกลกว่า)
    รองรับกรณีจำนวนวันของ d02/d01 เปลี่ยนไปในแต่ละวันที่ดาวน์โหลดมา (ไม่ hardcode จำนวนวัน)
    bias_correct: ถ้า True จะปรับสเกล d01 ด้วยอัตราส่วนเฉลี่ยจากวันที่ d01 กับ d02 มีข้อมูลซ้อนกันจริง
    """
    shapes = read_shapefile_polygons(shapefile_path)

    d02_means = _areal_mean_per_day(d02_path, shapes)
    d01_means = _areal_mean_per_day(d01_path, shapes)

    d02_dates_all = sorted(d02_means.index)
    d01_dates_all = sorted(d01_means.index)

    n_from_d02 = min(len(d02_dates_all), horizon)
    used_d02_dates = d02_dates_all[:n_from_d02]
    values_d02 = d02_means[used_d02_dates].values if used_d02_dates else []

    remaining = horizon - n_from_d02
    if remaining <= 0:
        return pd.Series(list(values_d02)[:horizon], index=used_d02_dates[:horizon])

    cutoff = used_d02_dates[-1] if used_d02_dates else None
    d01_future_dates = [d for d in d01_dates_all if cutoff is None or d > cutoff][:remaining]

    scale = 1.0
    if bias_correct and used_d02_dates:
        overlap_dates = [d for d in d01_dates_all if d in used_d02_dates]
        if overlap_dates:
            overlap_d01_vals = d01_means[overlap_dates].values
            overlap_d02_vals = d02_means[overlap_dates].values
            scale = (overlap_d01_vals / overlap_d02_vals).mean()

    values_d01 = d01_means[d01_future_dates].values / (scale if bias_correct else 1.0)

    combined_dates = used_d02_dates + d01_future_dates
    combined_values = list(values_d02) + list(values_d01)
    return pd.Series(combined_values, index=combined_dates)
