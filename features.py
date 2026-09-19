"""
features.py
สร้าง feature สำหรับเทรน/ทำนาย — ใช้ฟังก์ชันเดียวกันทั้งตอนเทรนและตอนพยากรณ์จริง
เพื่อไม่ให้ feature ตอนใช้งานจริงเพี้ยนไปจากตอนเทรน (training-serving skew)
"""
import numpy as np
import pandas as pd

INFLOW_LAGS = [1, 2, 3, 5, 7, 10, 14, 21, 30]
ROLL_WINDOWS = [3, 7, 14, 30]
STATE_COLS = ["level", "storage", "pct", "release"]
STATION_COLS = ["N1", "N13A", "N64"]
STATION_LAGS = [0, 1, 2, 3]
FORECAST_HORIZON = 7


def build_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """df ต้อง index เป็นวันที่ (daily, reindex ให้ครบปฏิทินและ interpolate ช่องว่างสั้นๆ มาก่อนแล้ว)
    ต้องมีคอลัมน์: inflow, level, storage, pct, release, และถ้ามี N1/N13A/N64 (สถานีน้ำท่า) จะใช้ด้วย
    คืนค่า DataFrame ของ feature เท่านั้น (ยังไม่รวม target)"""
    feat = pd.DataFrame(index=df.index)

    for lag in INFLOW_LAGS:
        feat[f"inflow_lag{lag}"] = df["inflow"].shift(lag)
    for w in ROLL_WINDOWS:
        feat[f"inflow_roll{w}_mean"] = df["inflow"].shift(1).rolling(w).mean()

    for col in STATE_COLS:
        if col in df.columns:
            feat[f"{col}_t0"] = df[col]
            feat[f"{col}_lag1"] = df[col].shift(1)
            feat[f"{col}_lag7"] = df[col].shift(7)

    for st_col in STATION_COLS:
        if st_col in df.columns:
            for lag in STATION_LAGS:
                feat[f"{st_col}_lag{lag}"] = df[st_col].shift(lag)
            feat[f"{st_col}_roll3_mean"] = df[st_col].rolling(3).mean()

    doy = df.index.dayofyear
    feat["doy_sin"] = np.sin(2 * np.pi * doy / 365.25)
    feat["doy_cos"] = np.cos(2 * np.pi * doy / 365.25)

    return feat


def build_targets(df: pd.DataFrame, horizon: int = FORECAST_HORIZON) -> pd.DataFrame:
    targets = pd.DataFrame(index=df.index)
    for h in range(1, horizon + 1):
        targets[f"inflow_t+{h}"] = df["inflow"].shift(-h)
    return targets


def build_training_dataset(df: pd.DataFrame, min_date=None):
    """รวม feature + target แล้วตัดแถวที่มีค่าขาด (NaN) ออก
    min_date: ตัดข้อมูลก่อนวันนี้ออก (เช่น ก่อนที่จะมีข้อมูลระดับน้ำ/สถานีครบ)"""
    if min_date is not None:
        df = df[df.index >= min_date]
    feat = build_feature_frame(df)
    targets = build_targets(df)
    full = pd.concat([feat, targets], axis=1).dropna()
    feature_cols = list(feat.columns)
    target_cols = list(targets.columns)
    return full, feature_cols, target_cols


def build_latest_feature_row(df: pd.DataFrame):
    """สร้าง feature ของ 'วันนี้' (แถวล่าสุด) ไว้ป้อนโมเดลตอนพยากรณ์จริง"""
    feat = build_feature_frame(df)
    return feat.iloc[[-1]]
