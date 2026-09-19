"""
app.py — เว็บพยากรณ์น้ำไหลเข้าอ่างเก็บน้ำเขื่อนสิริกิติ์ล่วงหน้า 7 วัน
รันด้วย: streamlit run app.py
"""
import datetime
import io
import tempfile
import zipfile
import pandas as pd
import streamlit as st

import data_io
import model as model_module
from station_parser import parse_station_file, parse_station_excel, cms_to_mcm_per_day
from reservoir_parser import parse_reservoir_excel
from hyd_parser import parse_hyd_workbook
import report as report_module
from rain_forecast_parser import parse_rainfall_forecast_excel
import rainfall as rainfall_module
from shp_reader import read_shapefile_polygons  # noqa: F401 (ใช้ตรวจสอบไฟล์ตอนอัปโหลด)

st.set_page_config(page_title="พยากรณ์น้ำไหลเข้าอ่างสิริกิติ์", page_icon="🌊", layout="wide")

RESERVOIR_COLS = ["capacity", "usable", "level", "storage", "pct", "inflow", "release"]
RESERVOIR_COLS_TH = {
    "capacity": "ความจุ รนก.",
    "usable": "ปริมาณน้ำใช้การ",
    "level": "ระดับน้ำในอ่าง",
    "storage": "ปริมาณน้ำในอ่าง",
    "pct": "% รนก.",
    "inflow": "ไหลลงอ่าง",
    "release": "ระบาย",
}
STATIONS = [("N1", "N.1"), ("N13A", "N.13A"), ("N64", "N.64")]


# ---------------------------------------------------------------------------
# ฟังก์ชันช่วยเตรียมข้อมูลสำหรับโมเดล (รวม reservoir + station)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner="กำลังเตรียมข้อมูลสำหรับโมเดล...")
def build_model_ready_df():
    res = data_io.load_all_reservoir_snapshots()
    if res.empty:
        return None
    res = res.set_index("date").sort_index()
    full_idx = pd.date_range(res.index.min(), res.index.max(), freq="D")
    res = res.reindex(full_idx)
    for col in RESERVOIR_COLS:
        res[col] = res[col].interpolate(method="linear", limit=3, limit_direction="both")

    # ข้อมูลสถานีน้ำท่า: รวม 3 แหล่ง — (1) ไฟล์ย้อนหลังที่นำเข้าครั้งแรก (.txt/.xlsx รายสถานี)
    # (2) ไฟล์ HYD workbook รวมหลายสถานี (เติมช่วงที่ไฟล์ (1) ยังไม่อัปเดตถึงปัจจุบัน)
    # (3) ตารางที่กรอกเองรายวัน — ถ้าวันไหนมีมากกว่า 1 แหล่ง ให้ใช้ตามลำดับ (3) > (2) > (1)
    snap = data_io.load_all_station_snapshots()
    if not snap.empty:
        snap = snap.set_index(pd.to_datetime(snap["date"])).sort_index()

    hyd_bytes = data_io.load_hyd_workbook_bytes()
    hyd_data = {}
    if hyd_bytes is not None:
        hyd_data = parse_hyd_workbook(hyd_bytes, dict(STATIONS))

    for code, _ in STATIONS:
        series_from_file = None
        raw, ext = data_io.load_station_file_bytes(code)
        if raw is not None:
            if ext == "txt":
                recs = parse_station_file(raw)
            else:
                recs = parse_station_excel(raw)
            s = pd.Series(recs).sort_index()
            s.index = pd.to_datetime(s.index)
            series_from_file = s.apply(cms_to_mcm_per_day)

        series_from_hyd = None
        if hyd_data.get(code):
            s = pd.Series(hyd_data[code]).sort_index()
            s.index = pd.to_datetime(s.index)
            series_from_hyd = s.apply(cms_to_mcm_per_day)

        series_from_manual = None
        if not snap.empty and f"{code}_Q" in snap.columns:
            q = pd.to_numeric(snap[f"{code}_Q"], errors="coerce").dropna()
            series_from_manual = q.apply(cms_to_mcm_per_day)

        parts = [p for p in [series_from_manual, series_from_hyd, series_from_file] if p is not None]
        if not parts:
            continue
        combined = parts[0]
        for p in parts[1:]:
            combined = combined.combine_first(p)

        res[code] = combined.reindex(res.index)

    # ข้อมูลฝนย้อนหลังที่กรอกเองรายวัน (สำหรับเทรนโมเดล)
    rain = data_io.load_all_rainfall_snapshots()
    if not rain.empty:
        rain_s = rain.set_index(pd.to_datetime(rain["date"]))["rainfall"]
        res["rainfall"] = rain_s.reindex(res.index)

    return res


# ---------------------------------------------------------------------------
# หน้า 1: พยากรณ์
# ---------------------------------------------------------------------------
def page_forecast():
    st.header("พยากรณ์น้ำไหลเข้าอ่าง 7 วันล่วงหน้า")

    df = build_model_ready_df()
    if df is None or len(df) < 60:
        st.warning("ยังไม่มีข้อมูลเพียงพอสำหรับพยากรณ์ (ต้องการอย่างน้อย 60 วัน) — ไปที่แท็บ 'อัปโหลดข้อมูลใหม่' แล้วใช้ช่อง 'นำเข้าข้อมูลย้อนหลัง' ก่อน")
        return

    last_date = df.index.max()
    st.caption(f"ข้อมูลล่าสุดที่มี: {last_date.strftime('%d %b %Y')}")

    # เช็คก่อนว่าโมเดลปัจจุบัน (ถ้ามี) ต้องใช้ฝนพยากรณ์ประกอบหรือไม่ — โชว์ตัวอัปโหลดไว้ก่อนกดปุ่ม
    _models_probe, _feat_probe, use_rainfall_probe = model_module.load_models_from_storage()
    rainfall_forecast = None
    forecast_dates_preview = [last_date + datetime.timedelta(days=i) for i in range(1, 8)]

    if use_rainfall_probe:
        st.markdown(
            "**โมเดลนี้ต้องการฝนคาดการณ์ 7 วันข้างหน้าประกอบการพยากรณ์** "
            f"(ตั้งแต่ {forecast_dates_preview[0].strftime('%d/%m/%Y')} ถึง {forecast_dates_preview[-1].strftime('%d/%m/%Y')})"
        )
        pending = data_io.load_rainfall_forecast()
        if pending.empty:
            st.warning(
                "ยังไม่มีฝนคาดการณ์ในระบบ — ไปที่แท็บ '📤 อัปโหลดข้อมูลใหม่' หัวข้อ 'ฝนคาดการณ์จากกรมอุตุฯ' "
                "เพื่ออัปโหลดก่อน แล้วกลับมาหน้านี้ใหม่"
            )
        else:
            pending_map = dict(zip(pending["date"].dt.date, pending["rainfall"]))
            rainfall_forecast = []
            missing = []
            for d in forecast_dates_preview:
                if d.date() in pending_map:
                    rainfall_forecast.append(float(pending_map[d.date()]))
                else:
                    missing.append(d.strftime("%d/%m/%Y"))
            if missing:
                st.error(
                    f"ฝนคาดการณ์ที่อัปโหลดไว้ไม่ครอบคลุมวันที่: {', '.join(missing)} — "
                    "ไปอัปโหลดไฟล์ใหม่ที่แท็บ 'อัปโหลดข้อมูลใหม่' ให้ครบ 7 วันจากวันที่ข้อมูลล่าสุด"
                )
                rainfall_forecast = None
            else:
                st.success("ใช้ฝนคาดการณ์ที่อัปโหลดไว้ล่าสุดจากแท็บอัปโหลดข้อมูล")
                st.dataframe(
                    pd.DataFrame(
                        {"วันที่": [d.strftime("%d/%m/%Y") for d in forecast_dates_preview], "ฝนคาดการณ์ (มม.)": rainfall_forecast}
                    ),
                    use_container_width=True,
                    hide_index=True,
                )

    col1, col2 = st.columns([1, 1])
    with col1:
        retrain = st.button("🔁 เทรนโมเดลใหม่ (ทำครั้งแรก และตอนมีข้อมูลใหม่เยอะขึ้น เช่น เดือนละครั้ง)")
    with col2:
        run_forecast = st.button("▶️ รันพยากรณ์ด้วยโมเดลปัจจุบัน", type="primary")

    if retrain:
        # แสดงความคืบหน้าเป็นราย horizon เพื่อให้ผู้ใช้เห็นว่าแอปยังทำงานอยู่
        progress = st.progress(0, text="กำลังเตรียมเทรนโมเดล 7 ตัว...")
        status = st.empty()

        def _on_train_progress(h, total, model):
            progress.progress(h / total, text=f"กำลังเทรนโมเดล {h}/{total} — Day +{h}")
            status.caption(
                f"Day +{h} เสร็จแล้ว • ใช้จริง {getattr(model, 'n_iter_', '-')} รอบจากสูงสุด {model_module.MODEL_PARAMS['max_iter']} รอบ"
            )

        models, metrics, feature_cols, use_rainfall = model_module.train_all_horizons(
            df, progress_callback=_on_train_progress
        )
        model_module.save_models_to_storage(models, feature_cols, use_rainfall)
        # โมเดลบน disk/Drive เปลี่ยนแล้ว ต้องล้าง cache ของตัวโหลดโมเดล
        model_module.load_models_from_storage.clear()
        progress.progress(1.0, text="เทรนโมเดลทั้ง 7 ตัวเสร็จแล้ว")
        status.empty()

        st.success("เทรนโมเดลใหม่เสร็จแล้ว" + (" (รวมข้อมูลฝนเป็น feature ด้วยแล้ว)" if use_rainfall else ""))
        st.dataframe(metrics.style.format({"mae": "{:.2f}", "rmse": "{:.2f}", "r2": "{:.3f}"}))
        if not use_rainfall and "rainfall" in df.columns:
            n_rain = df["rainfall"].notna().sum()
            st.caption(
                f"ℹ️ มีข้อมูลฝนแล้ว {n_rain} วัน แต่ยังไม่ถึงเกณฑ์ขั้นต่ำ ({model_module.RAIN_MIN_DAYS} วัน) "
                "จึงยังไม่ได้ใช้ฝนเป็น feature — กรอกฝนต่อไปเรื่อยๆ แล้วเทรนใหม่ภายหลัง"
            )
        if use_rainfall and not use_rainfall_probe:
            st.info("โมเดลเพิ่งเทรนใหม่โดยใช้ฝนเป็น feature ด้วย — กดปุ่มนี้อีกครั้ง (หรือรีเฟรชหน้า) เพื่อให้ช่องกรอกฝนพยากรณ์ปรากฏก่อนรันพยากรณ์")

    if run_forecast:
        models, feature_cols, use_rainfall = model_module.load_models_from_storage()
        if models is None:
            st.error("ยังไม่มีโมเดลที่เทรนไว้ — กดปุ่ม 'เทรนโมเดลใหม่' ก่อน (ต้องทำอย่างน้อยครั้งแรก)")
            return
        try:
            preds = model_module.predict_next_7_days(
                df, models, feature_cols, use_rainfall=use_rainfall, rainfall_forecast=rainfall_forecast
            )
        except ValueError as e:
            st.error(str(e))
            return
        forecast_dates = forecast_dates_preview

        data_io.save_forecast_log(last_date.date(), preds)

        chart_df = pd.DataFrame({"วันที่": [d.strftime("%d %b") for d in forecast_dates], "พยากรณ์ (ล้าน ลบ.ม./วัน)": preds})
        st.line_chart(chart_df.set_index("วันที่"))
        st.dataframe(chart_df, use_container_width=True, hide_index=True)

        st.info(
            "⚠️ ข้อควรระวัง: โมเดลนี้อาจพยากรณ์ต่ำกว่าความเป็นจริงในช่วงน้ำมาก/น้ำท่วมฉับพลัน "
            "ใช้ประกอบการตัดสินใจร่วมกับแหล่งข้อมูลอื่นเสมอ"
        )


# ---------------------------------------------------------------------------
# หน้า 2: อัปโหลดข้อมูลใหม่ — กรอกเป็นตารางรายวัน + นำเข้าย้อนหลังแยกต่างหาก
# ---------------------------------------------------------------------------
def _starter_reservoir_table():
    row = {"วันที่": datetime.date.today()}
    row.update({RESERVOIR_COLS_TH[c]: None for c in RESERVOIR_COLS})
    return pd.DataFrame([row])


def _starter_station_table():
    row = {"วันที่": datetime.date.today()}
    for code, label in STATIONS:
        row[f"{label} ระดับน้ำ (ม.)"] = None
        row[f"{label} ปริมาณน้ำ Q (ลบ.ม./วิ)"] = None
    return pd.DataFrame([row])


def _starter_rainfall_table():
    return pd.DataFrame([{"วันที่": datetime.date.today(), "ปริมาณฝนเฉลี่ยพื้นที่รับน้ำ (มม.)": None}])


def page_upload():
    st.header("อัปโหลดข้อมูลใหม่")

    # ---------------- ตารางข้อมูลอ่างเก็บน้ำ (อัปเดตรายวัน) ----------------
    st.subheader("ข้อมูลอ่างเก็บน้ำรายวัน")
    st.caption(
        "กรอกหรือ copy-paste ค่าจากตารางของกรมชลประทาน (RID) ใส่ในตารางด้านล่างได้เลย "
        "กด '+' ที่แถวล่างสุดของตารางเพื่อเพิ่มวัน (ข้อมูลเดิมจะไม่หาย)"
    )
    if "res_table" not in st.session_state:
        st.session_state.res_table = _starter_reservoir_table()

    edited_res = st.data_editor(
        st.session_state.res_table,
        use_container_width=True,
        num_rows="dynamic",
        hide_index=True,
        column_config={"วันที่": st.column_config.DateColumn("วันที่", format="DD/MM/YYYY")},
        key="res_editor",
    )
    st.session_state.res_table = edited_res

    if st.button("✅ ยืนยันบันทึกข้อมูลอ่าง", type="primary"):
        try:
            to_save = edited_res.rename(columns={v: k for k, v in RESERVOIR_COLS_TH.items()}).rename(
                columns={"วันที่": "date"}
            )
            to_save = to_save.dropna(subset=RESERVOIR_COLS, how="all")
            if to_save.isnull().values.any():
                st.error("มีช่องที่ยังไม่ได้กรอกอยู่ — กรอกให้ครบทุกช่องก่อนบันทึก")
            else:
                data_io.save_reservoir_snapshot(to_save)
                st.success(f"บันทึกข้อมูลอ่าง {len(to_save)} วันเรียบร้อยแล้ว")
                st.cache_data.clear()
        except Exception as e:
            st.error(f"บันทึกไม่สำเร็จ: {e}")

    st.divider()

    # ---------------- ตารางข้อมูลสถานีน้ำท่า (อัปเดตรายวัน) ----------------
    st.subheader("ข้อมูลสถานีน้ำท่าต้นน้ำรายวัน (N.1 / N.13A / N.64)")
    st.caption("กรอกระดับน้ำและปริมาณน้ำ (Q) ของแต่ละสถานี — ใช้ปรับพยากรณ์ให้แม่นขึ้นโดยเฉพาะ 1-3 วันแรก")
    if "st_table" not in st.session_state:
        st.session_state.st_table = _starter_station_table()

    edited_st = st.data_editor(
        st.session_state.st_table,
        use_container_width=True,
        num_rows="dynamic",
        hide_index=True,
        column_config={"วันที่": st.column_config.DateColumn("วันที่", format="DD/MM/YYYY")},
        key="st_editor",
    )
    st.session_state.st_table = edited_st

    if st.button("✅ ยืนยันบันทึกข้อมูลสถานีน้ำท่า", type="primary"):
        try:
            rename_map = {"วันที่": "date"}
            for code, label in STATIONS:
                rename_map[f"{label} ระดับน้ำ (ม.)"] = f"{code}_level"
                rename_map[f"{label} ปริมาณน้ำ Q (ลบ.ม./วิ)"] = f"{code}_Q"
            to_save = edited_st.rename(columns=rename_map)
            value_cols = [c for c in to_save.columns if c != "date"]
            to_save = to_save.dropna(subset=value_cols, how="all")
            data_io.save_station_snapshot(to_save)
            st.success(f"บันทึกข้อมูลสถานีน้ำท่า {len(to_save)} วันเรียบร้อยแล้ว")
            st.cache_data.clear()
        except Exception as e:
            st.error(f"บันทึกไม่สำเร็จ: {e}")

    st.divider()

    # ---------------- ฝนคาดการณ์จากกรมอุตุฯ (สำหรับนำไปรันโมเดล) ----------------
    st.subheader("🌦️ ฝนคาดการณ์จากกรมอุตุฯ (สำหรับนำไปรันพยากรณ์)")
    st.caption("เมื่อบันทึกแล้ว ระบบจะดึงไปใช้อัตโนมัติตอนกด 'รันพยากรณ์' ในแท็บพยากรณ์")

    rain_input_method = st.radio(
        "รูปแบบไฟล์ฝนคาดการณ์",
        ["ไฟล์กริดฝนพยากรณ์ (d01 + d02 .csv)", "ไฟล์ Excel ตัวเลขฝนสำเร็จรูป"],
        horizontal=True,
        key="rain_input_method",
    )

    if rain_input_method == "ไฟล์กริดฝนพยากรณ์ (d01 + d02 .csv)":
        shp_bytes = data_io.load_catchment_shp_bytes()
        if shp_bytes is None:
            st.error(
                "ยังไม่มีไฟล์ขอบเขตพื้นที่รับน้ำ (shapefile) ในระบบ — เลื่อนลงไปที่ช่อง "
                "'📁 อัปโหลดขอบเขตพื้นที่รับน้ำ / shapefile' ด้านล่างเพื่ออัปโหลดก่อน (ทำครั้งเดียว)"
            )
        else:
            c1, c2, c3 = st.columns([1, 1, 1])
            with c1:
                d02_file = st.file_uploader("ไฟล์ d02 (ละเอียด) .csv", type=["csv"], key="d02_uploader_upload")
            with c2:
                d01_file = st.file_uploader("ไฟล์ d01 (หยาบ, ครอบคลุมไกลกว่า) .csv", type=["csv"], key="d01_uploader_upload")
            with c3:
                bias_correct = st.checkbox("ปรับสเกล d01 ให้ต่อเนื่องกับ d02", value=False, key="bias_correct_upload")

            if d02_file is not None and d01_file is not None:
                try:
                    with tempfile.NamedTemporaryFile(suffix=".shp", delete=False) as tmp_shp:
                        tmp_shp.write(shp_bytes)
                        tmp_shp_path = tmp_shp.name
                    rain_series = rainfall_module.build_7day_rainfall(
                        d02_file, d01_file, tmp_shp_path, bias_correct=bias_correct
                    )
                    preview_df = pd.DataFrame(
                        {"วันที่": [d.strftime("%d/%m/%Y") for d in rain_series.index], "ฝนคาดการณ์ (มม.)": [round(v, 2) for v in rain_series.values]}
                    )
                    st.dataframe(preview_df, use_container_width=True, hide_index=True)
                    if st.button("✅ บันทึกฝนคาดการณ์นี้ไว้ใช้รันพยากรณ์", key="save_grid_rain"):
                        df_to_save = pd.DataFrame({"date": rain_series.index, "rainfall": rain_series.values})
                        data_io.save_rainfall_forecast(df_to_save)
                        st.success(f"บันทึกฝนคาดการณ์ {len(df_to_save)} วันเรียบร้อยแล้ว — ไปแท็บ 'พยากรณ์' เพื่อรันได้เลย")
                        st.cache_data.clear()
                except Exception as e:
                    st.error(f"ประมวลผลไฟล์ฝนไม่สำเร็จ: {e}")

    else:  # ไฟล์ Excel ตัวเลขฝนสำเร็จรูป
        st.caption("ไฟล์ .xlsx ต้องมีคอลัมน์หัวตารางที่มีคำว่า 'วันที่' และ 'ฝน' (เช่น 'ปริมาณฝนคาดการณ์ (มม.)')")
        rain_fc_file = st.file_uploader("ไฟล์ฝนคาดการณ์ (.xlsx)", type=["xlsx"], key="rain_fc_uploader")
        if rain_fc_file is not None:
            try:
                records = parse_rainfall_forecast_excel(rain_fc_file.read())
                preview_df = pd.DataFrame(
                    {"วันที่": [d.strftime("%d/%m/%Y") for d in sorted(records)], "ฝนคาดการณ์ (มม.)": [records[d] for d in sorted(records)]}
                )
                st.dataframe(preview_df, use_container_width=True, hide_index=True)
                if st.button("✅ บันทึกฝนคาดการณ์นี้ไว้ใช้รันพยากรณ์", key="save_excel_rain"):
                    df_to_save = pd.DataFrame({"date": list(records.keys()), "rainfall": list(records.values())})
                    data_io.save_rainfall_forecast(df_to_save)
                    st.success(f"บันทึกฝนคาดการณ์ {len(df_to_save)} วันเรียบร้อยแล้ว — ไปแท็บ 'พยากรณ์' เพื่อรันได้เลย")
                    st.cache_data.clear()
            except Exception as e:
                st.error(f"อ่านไฟล์ไม่สำเร็จ: {e}")

    st.divider()

    # ---------------- ตารางข้อมูลฝน (อัปเดตรายวัน — สำหรับสะสมไว้เทรนโมเดล) ----------------
    st.subheader("🌧️ ฝนตรวจวัดจริงรายวัน (สำหรับเก็บสะสมไว้เทรนโมเดล)")
    st.caption(
        f"กรอกปริมาณฝนเฉลี่ยพื้นที่รับน้ำของแต่ละวัน — ต้องสะสมอย่างน้อย {model_module.RAIN_MIN_DAYS} วัน "
        "โมเดลถึงจะเริ่มใช้ฝนเป็นตัวแปรช่วยพยากรณ์ (ดูสถานะได้ตอนกด 'เทรนโมเดลใหม่' ในแท็บพยากรณ์)"
    )
    if "rain_table" not in st.session_state:
        st.session_state.rain_table = _starter_rainfall_table()

    edited_rain = st.data_editor(
        st.session_state.rain_table,
        use_container_width=True,
        num_rows="dynamic",
        hide_index=True,
        column_config={"วันที่": st.column_config.DateColumn("วันที่", format="DD/MM/YYYY")},
        key="rain_editor",
    )
    st.session_state.rain_table = edited_rain

    if st.button("✅ ยืนยันบันทึกข้อมูลฝน", type="primary"):
        try:
            to_save = edited_rain.rename(
                columns={"วันที่": "date", "ปริมาณฝนเฉลี่ยพื้นที่รับน้ำ (มม.)": "rainfall"}
            )
            to_save = to_save.dropna(subset=["rainfall"])
            data_io.save_rainfall_snapshot(to_save)
            st.success(f"บันทึกข้อมูลฝน {len(to_save)} วันเรียบร้อยแล้ว")
            st.cache_data.clear()
        except Exception as e:
            st.error(f"บันทึกไม่สำเร็จ: {e}")

    st.divider()

    # ---------------- นำเข้าข้อมูลย้อนหลัง (ทำครั้งแรกตอน setup เท่านั้น) ----------------
    with st.expander("📁 นำเข้าข้อมูลอ่างย้อนหลัง (ทำครั้งแรกตอน setup เท่านั้น)"):
        st.caption(
            "อัปโหลดไฟล์ 'ตารางสรุปสภาพน้ำในเขื่อน...xlsx' ของกรมชลประทาน (มีข้อมูลย้อนหลังหลายปี) "
            "ครั้งเดียวตอนเริ่มต้น เพื่อให้โมเดลมีข้อมูลพอเทรน — ไม่ต้องทำซ้ำทุกวัน"
        )
        res_file = st.file_uploader("ไฟล์ข้อมูลอ่างย้อนหลัง (.xlsx)", type=["xlsx"], key="res_bulk_uploader")
        if res_file is not None and st.button("นำเข้าข้อมูลอ่างย้อนหลัง"):
            try:
                df_bulk = parse_reservoir_excel(res_file.read())
                data_io.save_reservoir_bulk(df_bulk)
                st.success(f"นำเข้าข้อมูลอ่างย้อนหลังสำเร็จ {len(df_bulk)} วัน ({df_bulk['date'].min()} ถึง {df_bulk['date'].max()})")
                st.cache_data.clear()
            except Exception as e:
                st.error(f"นำเข้าไม่สำเร็จ: {e}")

    with st.expander("📁 นำเข้าข้อมูลสถานีน้ำท่าย้อนหลัง (ทำครั้งแรกตอน setup เท่านั้น)"):
        st.caption(
            "รองรับทั้งไฟล์ .txt (รูปแบบรายงานของกรมชลประทาน) และ .xlsx (ตารางง่ายๆ ที่มีคอลัมน์ชื่อ 'วันที่' และ 'ปริมาณน้ำ'/'Q') "
            "อัปโหลดครั้งเดียวตอนเริ่มต้น — ใช้ตารางด้านบนสำหรับอัปเดตรายวันแทน"
        )
        station_code = st.selectbox("สถานี", [c for c, _ in STATIONS], format_func=lambda c: dict(STATIONS)[c])
        station_file = st.file_uploader("ไฟล์สถานีน้ำท่า (.txt หรือ .xlsx)", type=["txt", "xlsx"], key="station_uploader")
        if station_file is not None and st.button("นำเข้าข้อมูลสถานีย้อนหลัง"):
            try:
                ext = station_file.name.rsplit(".", 1)[-1].lower()
                content = station_file.read()
                # ทดสอบ parse ก่อนบันทึกจริง เพื่อเช็คว่าไฟล์อ่านได้
                if ext == "txt":
                    parse_station_file(content)
                else:
                    parse_station_excel(content)
                data_io.save_station_file(station_code, content, ext=ext)
                st.success(f"นำเข้าข้อมูลย้อนหลังของสถานี {dict(STATIONS)[station_code]} เรียบร้อยแล้ว")
                st.cache_data.clear()
            except Exception as e:
                st.error(f"นำเข้าไม่สำเร็จ: {e}")

        st.markdown("**หรือ** อัปโหลดไฟล์รวมหลายสถานีในไฟล์เดียว (เช่น 'Rainfall-Runoff-YYYY.xlsx' ที่มีชีต HYD1, HYD2, ...) "
                    "ใช้เติมช่วงวันที่ไฟล์ .txt รายสถานีด้านบนยังไม่อัปเดตถึงปัจจุบัน — ไม่ต้องเลือกสถานี ระบบหาให้เองทั้ง 3 สถานีในไฟล์เดียว")
        hyd_file = st.file_uploader("ไฟล์ HYD workbook (.xlsx)", type=["xlsx"], key="hyd_uploader")
        if hyd_file is not None and st.button("นำเข้าไฟล์ HYD workbook"):
            try:
                content = hyd_file.read()
                parsed = parse_hyd_workbook(content, dict(STATIONS))
                counts = {dict(STATIONS)[code]: len(vals) for code, vals in parsed.items()}
                if all(n == 0 for n in counts.values()):
                    st.error("อ่านไฟล์ได้ แต่ไม่พบข้อมูลของสถานี N.1 / N.13A / N.64 เลย — เช็คว่าไฟล์มีชีตและโครงสร้างตรงตามที่คาดไว้หรือไม่")
                else:
                    data_io.save_hyd_workbook(content)
                    st.success(f"นำเข้าไฟล์ HYD workbook สำเร็จ — จำนวนวันที่พบต่อสถานี: {counts}")
                    st.cache_data.clear()
            except Exception as e:
                st.error(f"นำเข้าไม่สำเร็จ: {e}")

    with st.expander("📁 อัปโหลดขอบเขตพื้นที่รับน้ำ / shapefile (ทำครั้งเดียวตอน setup เท่านั้น)"):
        st.caption(
            "ใช้ตัดพื้นที่รับน้ำสำหรับคำนวณฝนเฉลี่ยพื้นที่จากไฟล์กริดฝนพยากรณ์ (d01/d02) — ไม่ต้องอัปโหลดซ้ำทุกวัน "
            "เพราะขอบเขตพื้นที่ไม่เปลี่ยน อัปโหลดไฟล์ shapefile เป็น .zip (ต้องมีไฟล์ .shp อยู่ข้างใน)"
        )
        current_shp = data_io.load_catchment_shp_bytes()
        if current_shp is not None:
            st.success("มีขอบเขตพื้นที่รับน้ำในระบบแล้ว (อัปโหลดใหม่เพื่อแทนที่ได้)")
        shp_zip_file = st.file_uploader("ไฟล์ shapefile (.zip)", type=["zip"], key="shp_uploader")
        if shp_zip_file is not None and st.button("บันทึกขอบเขตพื้นที่รับน้ำ"):
            try:
                with zipfile.ZipFile(io.BytesIO(shp_zip_file.read())) as z:
                    shp_names = [n for n in z.namelist() if n.lower().endswith(".shp")]
                    if not shp_names:
                        raise ValueError("ไม่พบไฟล์ .shp อยู่ในไฟล์ zip นี้")
                    shp_bytes = z.read(shp_names[0])
                # ทดสอบ parse ก่อนบันทึกจริง เพื่อเช็คว่าเป็น Polygon shapefile ที่อ่านได้
                with tempfile.NamedTemporaryFile(suffix=".shp", delete=False) as tmp:
                    tmp.write(shp_bytes)
                    tmp_path = tmp.name
                read_shapefile_polygons(tmp_path)
                data_io.save_catchment_shp(shp_bytes)
                st.success("บันทึกขอบเขตพื้นที่รับน้ำเรียบร้อยแล้ว")
                st.cache_data.clear()
            except Exception as e:
                st.error(f"นำเข้าไม่สำเร็จ: {e}")


# ---------------------------------------------------------------------------
# หน้า 3: ประวัติ
# ---------------------------------------------------------------------------
def page_history():
    st.header("ประวัติข้อมูลและการพยากรณ์")

    st.subheader("ข้อมูลอ่างเก็บน้ำย้อนหลัง")
    res = data_io.load_all_reservoir_snapshots()
    if res.empty:
        st.info("ยังไม่มีข้อมูล")
    else:
        st.line_chart(res.set_index("date")["inflow"])
        display_res = res.rename(columns=RESERVOIR_COLS_TH).rename(columns={"date": "วันที่"})
        st.dataframe(display_res.sort_values("วันที่", ascending=False), use_container_width=True, hide_index=True)

    st.subheader("ข้อมูลสถานีน้ำท่าที่กรอกเองย้อนหลัง")
    snap = data_io.load_all_station_snapshots()
    if snap.empty:
        st.info("ยังไม่มีข้อมูล")
    else:
        st.dataframe(snap.sort_values("date", ascending=False), use_container_width=True, hide_index=True)

    st.subheader("ข้อมูลฝนที่กรอกเองย้อนหลัง")
    rain = data_io.load_all_rainfall_snapshots()
    if rain.empty:
        st.info("ยังไม่มีข้อมูล")
    else:
        st.caption(f"สะสมแล้ว {len(rain)} วัน จากที่ต้องการ {model_module.RAIN_MIN_DAYS} วันขึ้นไปเพื่อใช้เป็น feature")
        st.line_chart(rain.set_index("date")["rainfall"])
        st.dataframe(rain.sort_values("date", ascending=False), use_container_width=True, hide_index=True)

    st.subheader("ประวัติการพยากรณ์ที่เคยรันไว้")
    logs = data_io.load_all_forecast_logs()
    if logs.empty:
        st.info("ยังไม่มีประวัติการพยากรณ์")
    else:
        st.dataframe(logs.sort_values("run_date", ascending=False), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# หน้า 4: กราฟสรุป — ปริมาณน้ำในอ่างย้อนหลัง ต่อเนื่องถึงคาดการณ์ 7 วัน พร้อมฝน
# ---------------------------------------------------------------------------
def page_dashboard():
    import plotly.graph_objects as go

    st.header("กราฟสรุปปริมาณน้ำในอ่าง: ย้อนหลัง + คาดการณ์ 7 วัน")

    df = build_model_ready_df()
    if df is None or len(df) < 60:
        st.warning("ยังไม่มีข้อมูลเพียงพอ — ไปที่แท็บ 'อัปโหลดข้อมูลใหม่' ก่อน")
        return

    models, feature_cols, use_rainfall = model_module.load_models_from_storage()
    if models is None:
        st.warning("ยังไม่มีโมเดลที่เทรนไว้ — ไปที่แท็บ 'พยากรณ์' แล้วกด 'เทรนโมเดลใหม่' ก่อน")
        return

    last_date = df.index.max()
    forecast_dates = [last_date + datetime.timedelta(days=i) for i in range(1, 8)]

    rainfall_forecast = None
    if use_rainfall:
        pending = data_io.load_rainfall_forecast()
        if not pending.empty:
            pending_map = dict(zip(pending["date"].dt.date, pending["rainfall"]))
            if all(d.date() in pending_map for d in forecast_dates):
                rainfall_forecast = [float(pending_map[d.date()]) for d in forecast_dates]
        if rainfall_forecast is None:
            st.warning(
                "โมเดลนี้ต้องการฝนคาดการณ์ครบ 7 วันจึงจะพยากรณ์ได้ — ไปอัปโหลดที่แท็บ 'อัปโหลดข้อมูลใหม่' ก่อน"
            )
            return

    try:
        preds = model_module.predict_next_7_days(
            df, models, feature_cols, use_rainfall=use_rainfall, rainfall_forecast=rainfall_forecast
        )
    except ValueError as e:
        st.error(str(e))
        return

    lookback_days = st.slider("จำนวนวันย้อนหลังที่แสดงในกราฟ", 7, 60, 15)
    hist = df.loc[last_date - datetime.timedelta(days=lookback_days - 1):last_date]

    recent_release_avg = float(df["release"].tail(7).mean())
    assumed_release = st.number_input(
        "สมมติฐานปริมาณน้ำระบายต่อวันช่วงคาดการณ์ (ล้าน ลบ.ม./วัน) — ค่าเริ่มต้นคือเฉลี่ย 7 วันล่าสุดที่มีจริง",
        min_value=0.0, value=round(recent_release_avg, 2), step=0.5,
    )

    capacity = float(df["capacity"].iloc[-1])
    last_storage = float(df["storage"].iloc[-1])
    storage_forecast = []
    running = last_storage
    for p in preds:
        running = running + p - assumed_release
        storage_forecast.append(running)

    # ฝนสำหรับแสดงผล (ถ้ามี) — ไม่จำเป็นต้องเป็นตัวเดียวกับที่ใช้รันโมเดล เอาไว้ดูประกอบกราฟ
    rain_hist = hist["rainfall"] if "rainfall" in hist.columns else pd.Series(dtype=float)
    rain_fc_display = rainfall_forecast
    if rain_fc_display is None:
        pending = data_io.load_rainfall_forecast()
        if not pending.empty:
            pending_map = dict(zip(pending["date"].dt.date, pending["rainfall"]))
            rain_fc_display = [pending_map.get(d.date()) for d in forecast_dates]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=hist.index, y=hist["storage"], mode="lines+markers",
        name="ปริมาณน้ำในอ่างจริง (ล้าน ลบ.ม.)", line=dict(color="#1f77b4"),
    ))
    fig.add_trace(go.Scatter(
        x=[last_date] + forecast_dates, y=[last_storage] + storage_forecast, mode="lines+markers",
        name="ปริมาณน้ำในอ่างคาดการณ์ (ล้าน ลบ.ม.)", line=dict(color="#d62728", dash="dash"),
    ))
    fig.add_trace(go.Scatter(
        x=[hist.index.min(), forecast_dates[-1]], y=[capacity, capacity], mode="lines",
        name=f"ความจุเก็บกัก ({capacity:,.2f} ล้าน ลบ.ม.)", line=dict(color="#d62728", dash="dot"),
    ))
    if not rain_hist.dropna().empty:
        fig.add_trace(go.Bar(
            x=rain_hist.index, y=rain_hist.values, name="ฝนจริงเฉลี่ย (มม.)",
            marker_color="#4c78a8", opacity=0.55, yaxis="y2",
        ))
    if rain_fc_display and any(v is not None for v in rain_fc_display):
        fig.add_trace(go.Bar(
            x=forecast_dates, y=rain_fc_display, name="ฝนคาดการณ์เฉลี่ย (มม.)",
            marker_color="#f58518", opacity=0.55, yaxis="y2",
        ))

    fig.update_layout(
        yaxis=dict(title="ปริมาณน้ำในอ่าง (ล้าน ลบ.ม.)"),
        yaxis2=dict(title="ฝน (มม.)", overlaying="y", side="right", autorange="reversed", showgrid=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        hovermode="x unified",
        margin=dict(t=10),
    )
    st.plotly_chart(fig, use_container_width=True)

    table_df = pd.DataFrame({
        "วันที่": [d.strftime("%d/%m/%Y") for d in forecast_dates],
        "ไหลลงอ่างคาดการณ์ (ล้าน ลบ.ม./วัน)": [round(p, 2) for p in preds],
        "ฝนคาดการณ์ (มม.)": [round(v, 2) if v is not None else None for v in (rain_fc_display or [None] * 7)],
        "ปริมาณน้ำในอ่างคาดการณ์ (ล้าน ลบ.ม.)": [round(s, 2) for s in storage_forecast],
    })
    st.dataframe(table_df, use_container_width=True, hide_index=True)

    st.info(
        "หมายเหตุ: ไม่มีเส้น Upper Rule Curve เพราะไม่มีข้อมูลเกณฑ์ปฏิบัติการอ่างของเขื่อนสิริกิติ์ในระบบ "
        "และปริมาณน้ำในอ่างคาดการณ์คำนวณจากสมดุลน้ำอย่างง่าย (น้ำเดิม + ไหลเข้าคาดการณ์ - ระบายตามสมมติฐานด้านบน) "
        "ไม่ใช่ผลจากโมเดล ML โดยตรง หากปริมาณระบายจริงเปลี่ยนไปจากที่ตั้งไว้ ตัวเลขนี้จะคลาดเคลื่อน"
    )

    st.divider()
    st.subheader("📥 รายงานสรุป (ดาวน์โหลดเป็นรูปภาพ)")

    summary_text = report_module.build_summary_text(
        "เขื่อนสิริกิติ์", last_date, last_storage, capacity, forecast_dates, preds, assumed_release, storage_forecast
    )
    st.markdown(
        f'<div style="background-color:#eaf4fb;padding:12px;border-radius:6px;line-height:1.7">{summary_text.replace(chr(10), "<br>")}</div>',
        unsafe_allow_html=True,
    )

    if st.button("🖼️ สร้างรายงานเป็นรูปภาพ"):
        with st.spinner("กำลังสร้างรูปภาพ..."):
            png_bytes, font_used = report_module.generate_report_image(
                "เขื่อนสิริกิติ์", hist, forecast_dates, preds, storage_forecast,
                capacity, rain_hist, rain_fc_display, last_date, last_storage,
            )
        if font_used is None:
            st.warning(
                "ไม่พบฟอนต์ไทยในเครื่องนี้ (ลองหา Tahoma, Leelawadee UI, TH Sarabun New) "
                "ตัวอักษรไทยในภาพอาจแสดงผลไม่ถูกต้อง"
            )
        st.image(png_bytes, use_container_width=True)
        st.download_button(
            "⬇️ ดาวน์โหลดรูปภาพ (.png)", data=png_bytes,
            file_name=f"รายงานสิริกิติ์_{last_date.strftime('%Y%m%d')}.png", mime="image/png",
        )


# ---------------------------------------------------------------------------
st.title("🌊 ระบบพยากรณ์น้ำไหลเข้าอ่างเก็บน้ำเขื่อนสิริกิติ์")

if data_io.storage_mode() == "local":
    st.sidebar.warning("⚠️ กำลังใช้ local storage (ยังไม่ได้ตั้งค่า Google Drive) — ข้อมูลจะหายถ้า deploy ใหม่")
else:
    st.sidebar.success("✅ เชื่อมต่อ Google Drive แล้ว")

# ใช้ lazy loading: Streamlit จะรันเฉพาะแท็บที่กำลังเปิดอยู่
# ช่วยลดเวลารอ เพราะแต่ละหน้ามีการอ่าน/ประมวลผลข้อมูลหลายชุด
tab1, tab2, tab3, tab4 = st.tabs(
    ["📈 พยากรณ์", "📤 อัปโหลดข้อมูลใหม่", "🗂️ ประวัติ", "📊 กราฟสรุป"],
    on_change="rerun",
    key="main_tabs",
)

if tab1.open:
    with tab1:
        page_forecast()

if tab2.open:
    with tab2:
        page_upload()

if tab3.open:
    with tab3:
        page_history()

if tab4.open:
    with tab4:
        page_dashboard()
