"""
model.py
เทรนโมเดลแยก 7 ตัว (1 ตัวต่อ 1 horizon วัน) ด้วย HistGradientBoostingRegressor
เลือกใช้ตัวนี้แทน XGBoost เพราะเป็นไลบรารีในตัวของ scikit-learn ไม่ต้องติดตั้งเพิ่ม
และให้ผลลัพธ์ใกล้เคียงกันมากสำหรับข้อมูลขนาดนี้ (ทดสอบเทียบไว้แล้วตอนพัฒนา)

เรื่องฝน: ถ้ามีข้อมูลฝนย้อนหลังเพียงพอ (>= RAIN_MIN_DAYS วัน) จะเพิ่ม feature 'rain_fcst'
ต่อ horizon — ตอนเทรนใช้ 'ฝนจริงของวันเป้าหมาย' เป็นตัวแทนของพยากรณ์ฝนที่สมบูรณ์แบบ
ตอนพยากรณ์จริงต้องป้อนค่าฝนพยากรณ์ 7 วันข้างหน้าเข้ามาแทน (ดู predict_next_7_days)
"""
import io
import joblib
import numpy as np
import pandas as pd
import streamlit as st
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

import data_io
from features import build_feature_frame, build_latest_feature_row, FORECAST_HORIZON

# ปรับให้เหมาะกับ Streamlit Community Cloud:
# - ลดจำนวนรอบสูงสุดจาก 300 -> 150
# - ลดความลึกจาก 6 -> 5
# - เปิด early stopping เพื่อหยุดเองเมื่อผลไม่ดีขึ้น
MODEL_PARAMS = dict(
    max_iter=150,
    max_depth=5,
    learning_rate=0.05,
    early_stopping=True,
    n_iter_no_change=10,
    validation_fraction=0.1,
    random_state=42,
)
RAIN_MIN_DAYS = 90  # ต้องมีข้อมูลฝนย้อนหลังอย่างน้อยเท่านี้ก่อนจะเริ่มใช้เป็น feature


def train_all_horizons(
    df: pd.DataFrame,
    min_date="2006-01-01",
    test_frac=0.15,
    progress_callback=None,
):
    """เทรนโมเดลทั้ง 7 horizon คืนค่า (models, metrics, feature_cols, use_rainfall)
    ใช้ time-based split (ท้ายสุดของข้อมูลเป็น test) ห้ามสุ่มแบ่ง

    progress_callback(h, total, model) จะถูกเรียกหลังเทรนแต่ละ horizon
    เพื่อให้หน้าเว็บแสดงความคืบหน้าได้โดยไม่ต้องเปลี่ยนวิธีการพยากรณ์เดิม
    """
    df = df[df.index >= min_date]
    feat_base = build_feature_frame(df)

    use_rainfall = "rainfall" in df.columns and df["rainfall"].notna().sum() >= RAIN_MIN_DAYS

    models = {}
    rows = []
    feature_cols = list(feat_base.columns) + (["rain_fcst"] if use_rainfall else [])

    for h in range(1, FORECAST_HORIZON + 1):
        feat = feat_base.copy()
        if use_rainfall:
            # ตอนเทรน: ใช้ฝน 'จริง' ของวันเป้าหมาย (t+h) แทนพยากรณ์ที่สมบูรณ์แบบ
            feat["rain_fcst"] = df["rainfall"].shift(-h)
        target = df["inflow"].shift(-h).rename("target")
        combined = pd.concat([feat, target], axis=1).dropna()

        n_test = max(30, int(len(combined) * test_frac))
        train = combined.iloc[:-n_test]
        test = combined.iloc[-n_test:]

        model = HistGradientBoostingRegressor(**MODEL_PARAMS)
        model.fit(train[feature_cols], train["target"])
        pred_test = model.predict(test[feature_cols])
        rows.append(
            {
                "horizon": h,
                "n_train": len(train),
                "mae": mean_absolute_error(test["target"], pred_test),
                "rmse": np.sqrt(mean_squared_error(test["target"], pred_test)),
                "r2": r2_score(test["target"], pred_test),
            }
        )
        models[h] = model
        if progress_callback is not None:
            progress_callback(h, FORECAST_HORIZON, model)

    metrics = pd.DataFrame(rows)
    return models, metrics, feature_cols, use_rainfall


def save_models_to_storage(models: dict, feature_cols: list, use_rainfall: bool):
    buf = io.BytesIO()
    joblib.dump(
        {"models": models, "feature_cols": feature_cols, "use_rainfall": use_rainfall}, buf
    )
    data_io.save_models(buf.getvalue())


@st.cache_resource(show_spinner="กำลังโหลดโมเดลพยากรณ์...")
def load_models_from_storage():
    raw = data_io.load_models_bytes()
    if raw is None:
        return None, None, False
    obj = joblib.load(io.BytesIO(raw))
    return obj["models"], obj["feature_cols"], obj.get("use_rainfall", False)


def predict_next_7_days(df: pd.DataFrame, models: dict, feature_cols: list, use_rainfall=False, rainfall_forecast=None):
    """ทำนาย inflow 7 วันข้างหน้าจากข้อมูลล่าสุดใน df
    rainfall_forecast: list ความยาว 7 (ฝนพยากรณ์ วันที่ 1..7 ข้างหน้า) — จำเป็นถ้า use_rainfall=True"""
    if use_rainfall and (rainfall_forecast is None or len(rainfall_forecast) < FORECAST_HORIZON):
        raise ValueError("โมเดลนี้เทรนโดยใช้ข้อมูลฝน ต้องกรอกฝนพยากรณ์ให้ครบ 7 วันก่อนพยากรณ์")

    latest_base = build_latest_feature_row(df)
    preds = []
    for h in range(1, FORECAST_HORIZON + 1):
        row = latest_base.copy()
        if use_rainfall:
            row["rain_fcst"] = rainfall_forecast[h - 1]
        row = row[feature_cols]
        preds.append(float(models[h].predict(row)[0]))
    return preds
