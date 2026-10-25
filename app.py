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
import baseflow as baseflow_module
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
    if not _require_login():
        return

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
        with st.spinner("กำลังเทรนโมเดล 7 ตัว (1 ตัวต่อ 1 วันล่วงหน้า)..."):
            models, metrics, feature_cols, use_rainfall = model_module.train_all_horizons(df)
            model_module.save_models_to_storage(models, feature_cols, use_rainfall)
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

        # log เก็บผลดิบจากโมเดลเสมอ (ไม่รวมค่าปรับ) เพื่อใช้ประเมินความแม่นของโมเดลภายหลัง
        data_io.save_forecast_log(last_date.date(), preds)

        preds_adj, adj_mcm = baseflow_module.apply_adjustment(preds, forecast_dates)
        day_labels = [d.strftime("%d %b") for d in forecast_dates]
        if baseflow_module.has_adjustment(adj_mcm):
            chart_df = pd.DataFrame({
                "วันที่": day_labels,
                "โมเดล (ล้าน ลบ.ม./วัน)": preds,
                "ปรับ Baseflow แล้ว (ล้าน ลบ.ม./วัน)": preds_adj,
            })
            st.line_chart(chart_df.set_index("วันที่"))
            table_adj = chart_df.copy()
            table_adj["ส่วนที่ปรับ (ล้าน ลบ.ม./วัน)"] = adj_mcm
            st.dataframe(table_adj.round(2), use_container_width=True, hide_index=True)
            st.info(
                "🔧 ผลพยากรณ์ด้านบนมีการปรับค่า Baseflow เพิ่มโดยผู้ดูแลระบบ — "
                "คอลัมน์ 'โมเดล' คือผลดิบจากโมเดล ส่วน 'ปรับ Baseflow แล้ว' คือค่าที่ใช้ในกราฟสรุป/รายงาน"
            )
        else:
            chart_df = pd.DataFrame({"วันที่": day_labels, "พยากรณ์ (ล้าน ลบ.ม./วัน)": preds})
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

    # ใช้ form: แก้ตารางได้อิสระโดยหน้าเว็บไม่รีโหลด/ไม่บันทึก จนกว่าจะกดปุ่มบันทึก
    with st.form("res_form"):
        edited_res = st.data_editor(
            st.session_state.res_table,
            use_container_width=True,
            num_rows="dynamic",
            hide_index=True,
            column_config={"วันที่": st.column_config.DateColumn("วันที่", format="DD/MM/YYYY")},
            key="res_editor",
        )
        save_res = st.form_submit_button("✅ ยืนยันบันทึกข้อมูลอ่าง", type="primary")

    if save_res:
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
    st.caption("กรอกปริมาณน้ำ (Q) ของแต่ละสถานี — ใช้ปรับพยากรณ์ให้แม่นขึ้นโดยเฉพาะ 1-3 วันแรก")
    if "st_table" not in st.session_state:
        st.session_state.st_table = _starter_station_table()

    with st.form("st_form"):
        edited_st = st.data_editor(
            st.session_state.st_table,
            use_container_width=True,
            num_rows="dynamic",
            hide_index=True,
            column_config={"วันที่": st.column_config.DateColumn("วันที่", format="DD/MM/YYYY")},
            key="st_editor",
        )
        save_st = st.form_submit_button("✅ ยืนยันบันทึกข้อมูลสถานีน้ำท่า", type="primary")

    if save_st:
        try:
            rename_map = {"วันที่": "date"}
            for code, label in STATIONS:
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

    with st.form("rain_form"):
        edited_rain = st.data_editor(
            st.session_state.rain_table,
            use_container_width=True,
            num_rows="dynamic",
            hide_index=True,
            column_config={"วันที่": st.column_config.DateColumn("วันที่", format="DD/MM/YYYY")},
            key="rain_editor",
        )
        save_rain = st.form_submit_button("✅ ยืนยันบันทึกข้อมูลฝน", type="primary")

    if save_rain:
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
    if not _require_login():
        return

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

    # ผลดิบจากโมเดล -> บวกค่าปรับ Baseflow ที่ผู้ดูแลตั้งไว้ (ถ้ามี) ก่อนนำไปคำนวณสมดุลน้ำ/รายงาน
    preds_model = preds
    preds, adj_mcm = baseflow_module.apply_adjustment(preds_model, forecast_dates)
    is_adjusted = baseflow_module.has_adjustment(adj_mcm)
    if is_adjusted:
        st.info("🔧 กราฟและรายงานนี้ใช้ค่าไหลลงอ่างที่ปรับ Baseflow เพิ่มแล้วโดยผู้ดูแลระบบ (ดูผลดิบจากโมเดลในตารางด้านล่าง)")

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

    table_cols = {"วันที่": [d.strftime("%d/%m/%Y") for d in forecast_dates]}
    if is_adjusted:
        table_cols["ผลโมเดลก่อนปรับ (ล้าน ลบ.ม./วัน)"] = [round(p, 2) for p in preds_model]
        table_cols["ปรับ Baseflow (ล้าน ลบ.ม./วัน)"] = [round(a, 2) for a in adj_mcm]
    table_cols["ไหลลงอ่างคาดการณ์ (ล้าน ลบ.ม./วัน)"] = [round(p, 2) for p in preds]
    table_cols["ฝนคาดการณ์ (มม.)"] = [round(v, 2) if v is not None else None for v in (rain_fc_display or [None] * 7)]
    table_cols["ปริมาณน้ำในอ่างคาดการณ์ (ล้าน ลบ.ม.)"] = [round(s, 2) for s in storage_forecast]
    table_df = pd.DataFrame(table_cols)
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
    if is_adjusted:
        summary_text += "\n(หมายเหตุ: ค่าไหลลงอ่างรวมนี้ปรับ Baseflow เพิ่มโดยผู้ดูแลระบบแล้ว)"
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
# หน้า 5: หลังบ้าน — ปรับ Baseflow รายวัน (ต้องล็อกอินด้วยบัญชีผู้ดูแล)
# ---------------------------------------------------------------------------
BF_COL_DATE = "วันที่"
BF_COL_ADJ = "ปรับเพิ่ม Baseflow (ลบ.ม./วิ)"
BF_COL_NOTE = "หมายเหตุ"
BF_WARN_CMS = 300  # เตือนถ้าค่าปรับต่อวันเกินนี้ (ลบ.ม./วิ) — กันกรอกผิดหน่วย


def render_login_box():
    """ช่องเข้าสู่ระบบกลางของทั้งแอป (แสดงบนสุดของหน้า ก่อนแท็บ) — ต้องล็อกอินก่อนรันโมเดล"""
    if baseflow_module.is_logged_in():
        role_th = "ผู้ดูแล" if baseflow_module.is_admin() else "ผู้ใช้"
        c1, c2 = st.columns([6, 1])
        c1.success(f"🔐 เข้าสู่ระบบเป็น {baseflow_module.current_user()} ({role_th})")
        if c2.button("ออกจากระบบ", use_container_width=True):
            baseflow_module.logout()
            st.rerun()
        return
    if not baseflow_module.accounts_configured():
        st.warning(
            "ยังไม่ได้ตั้งบัญชีผู้ใช้ — ไปที่ App settings → Secrets แล้วเพิ่มหัวข้อ `[users]` และ/หรือ `[admin_users]` "
            "(ดูตัวอย่างใน README) ระบบจึงจะอนุญาตให้รันโมเดลได้"
        )
        return
    with st.expander("🔐 เข้าสู่ระบบ (ต้องเข้าสู่ระบบก่อนจึงจะรันโมเดลได้)", expanded=True):
        if baseflow_module.login_locked():
            st.error("ใส่รหัสผ่านผิดเกินจำนวนครั้งที่กำหนด — รีเฟรชหน้าเว็บแล้วลองใหม่")
            return
        with st.form("login_form"):
            u = st.text_input("ชื่อผู้ใช้")
            pw = st.text_input("รหัสผ่าน", type="password")
            submitted = st.form_submit_button("เข้าสู่ระบบ", type="primary")
        if submitted:
            if baseflow_module.login(u, pw):
                st.rerun()
            else:
                st.error("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")


def _require_login() -> bool:
    """ใช้ต้นหน้าที่รันโมเดล: คืน True ถ้าล็อกอินแล้ว ไม่งั้นแสดงข้อความและคืน False"""
    if baseflow_module.is_logged_in():
        return True
    if not baseflow_module.accounts_configured():
        st.warning("ยังไม่ได้ตั้งบัญชีผู้ใช้ในระบบ จึงยังรันโมเดลไม่ได้ — ดูวิธีตั้งค่าใน README")
    else:
        st.info("🔐 ต้องเข้าสู่ระบบก่อนจึงจะรันโมเดลได้ — เข้าสู่ระบบที่ช่องบนสุดของหน้า")
    return False


def _preview_model_preds(forecast_dates):
    """รันโมเดลปัจจุบันเพื่อแสดงตัวอย่างก่อน/หลังปรับ คืน (preds, error_message)"""
    df = build_model_ready_df()
    if df is None or len(df) < 60:
        return None, "ยังไม่มีข้อมูลอ่างเพียงพอสำหรับพยากรณ์"
    models, feature_cols, use_rainfall = model_module.load_models_from_storage()
    if models is None:
        return None, "ยังไม่มีโมเดลที่เทรนไว้"
    rainfall_forecast = None
    if use_rainfall:
        pending = data_io.load_rainfall_forecast()
        if pending.empty:
            return None, "โมเดลต้องใช้ฝนคาดการณ์ แต่ยังไม่มีฝนคาดการณ์ในระบบ"
        pending_map = dict(zip(pending["date"].dt.date, pending["rainfall"]))
        if not all(d.date() in pending_map for d in forecast_dates):
            return None, "ฝนคาดการณ์ที่อัปโหลดไว้ไม่ครอบคลุม 7 วันข้างหน้า"
        rainfall_forecast = [float(pending_map[d.date()]) for d in forecast_dates]
    try:
        preds = model_module.predict_next_7_days(
            df, models, feature_cols, use_rainfall=use_rainfall, rainfall_forecast=rainfall_forecast
        )
    except ValueError as e:
        return None, str(e)
    return preds, None


def page_admin():
    st.header("🔧 หลังบ้าน: ปรับค่า Baseflow รายวัน")
    user = baseflow_module.current_admin()
    if not user:
        if baseflow_module.is_logged_in():
            st.warning("บัญชีนี้เป็นผู้ใช้ทั่วไป — หน้านี้ต้องใช้บัญชีผู้ดูแล (แอดมิน) เท่านั้น")
        else:
            st.info("🔐 หน้านี้สำหรับผู้ดูแลระบบ — เข้าสู่ระบบด้วยบัญชีแอดมินที่ช่องบนสุดของหน้า")
        return

    st.caption(
        "ค่าที่กรอกคือ **ปริมาณน้ำที่บวกเพิ่ม** จากผลโมเดลในแต่ละวัน หน่วย ลบ.ม./วินาที (cms) "
        "ระบบแปลงเป็น ล้าน ลบ.ม./วัน ให้เอง (1 cms = 0.0864 ล้าน ลบ.ม./วัน) "
        "ใส่ค่าลบได้ถ้าต้องการลด และผลรวมต่อวันจะไม่ต่ำกว่า 0"
    )

    df = build_model_ready_df()
    if df is None or df.empty:
        st.warning("ยังไม่มีข้อมูลอ่างในระบบ — ใช้วันที่ตั้งแต่พรุ่งนี้เป็นฐานแทนไปก่อน")
        last_date = pd.Timestamp.today().normalize()
    else:
        last_date = df.index.max()
    forecast_dates = [last_date + datetime.timedelta(days=i) for i in range(1, 8)]
    st.caption(
        f"ข้อมูลล่าสุด {last_date.strftime('%d/%m/%Y')} → ปรับได้ {forecast_dates[0].strftime('%d/%m/%Y')} "
        f"ถึง {forecast_dates[-1].strftime('%d/%m/%Y')}"
    )

    saved = data_io.load_baseflow_adjustments()
    saved_map = {}
    for _, r in saved.iterrows():
        note = "" if pd.isna(r["note"]) else str(r["note"])
        saved_map[r["date"].date()] = (float(r["adj_cms"]), note)

    # ตั้งต้นตารางจากค่าที่บันทึกไว้ (ทำใหม่เมื่อวันที่ข้อมูลล่าสุดเปลี่ยน)
    sig = last_date.strftime("%Y-%m-%d")
    if "bf_table" not in st.session_state or st.session_state.get("bf_sig") != sig:
        st.session_state.bf_table = pd.DataFrame({
            BF_COL_DATE: [d.date() for d in forecast_dates],
            BF_COL_ADJ: [saved_map.get(d.date(), (0.0, ""))[0] for d in forecast_dates],
            BF_COL_NOTE: [saved_map.get(d.date(), (0.0, ""))[1] for d in forecast_dates],
        })
        st.session_state.bf_sig = sig
        st.session_state.bf_ver = st.session_state.get("bf_ver", 0) + 1

    # ใช้ form: แก้ตารางได้ต่อเนื่องโดยหน้าไม่รีโหลด จนกว่าจะกด "ดูตัวอย่าง" หรือ "บันทึก"
    with st.form("bf_form"):
        edited = st.data_editor(
            st.session_state.bf_table,
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            column_config={
                BF_COL_DATE: st.column_config.DateColumn(BF_COL_DATE, format="DD/MM/YYYY", disabled=True),
                BF_COL_ADJ: st.column_config.NumberColumn(
                    BF_COL_ADJ, step=0.5, format="%.2f", min_value=-2000.0, max_value=2000.0,
                    help="ค่าบวก = เพิ่มน้ำไหลลงอ่าง, ค่าลบ = ลด",
                ),
                BF_COL_NOTE: st.column_config.TextColumn(BF_COL_NOTE, max_chars=200),
            },
            key=f"bf_editor_{st.session_state.bf_ver}",
        )
        f1, f2, f3 = st.columns([2, 1, 1])
        fill_val = f1.number_input("ค่าเดียวกันทุกวัน (ลบ.ม./วิ)", value=0.0, step=1.0)
        fill_clicked = f2.form_submit_button("ใส่ค่านี้ทุกวัน", use_container_width=True)
        zero_clicked = f3.form_submit_button("ล้างเป็น 0", use_container_width=True)
        g1, g2 = st.columns(2)
        preview_clicked = g1.form_submit_button("👁️ ดูตัวอย่างผลหลังปรับ", use_container_width=True)
        save_clicked = g2.form_submit_button("💾 บันทึกค่าปรับ", type="primary", use_container_width=True)

    if fill_clicked or zero_clicked:
        new_tbl = edited.copy()  # คงหมายเหตุที่พิมพ์ไว้
        new_tbl[BF_COL_ADJ] = float(fill_val) if fill_clicked else 0.0
        st.session_state.bf_table = new_tbl
        st.session_state.bf_ver += 1
        st.rerun()

    adj_cms = pd.to_numeric(edited[BF_COL_ADJ], errors="coerce").fillna(0.0)
    notes = edited[BF_COL_NOTE].fillna("").astype(str)
    adj_edit_map = {d.date(): float(v) for d, v in zip(forecast_dates, adj_cms)}

    if (adj_cms.abs() > BF_WARN_CMS).any():
        st.warning(f"มีค่าปรับเกิน {BF_WARN_CMS} ลบ.ม./วิ ต่อวัน — ตรวจสอบหน่วยอีกครั้งก่อนบันทึก")

    # ---- ตัวอย่างผลก่อน/หลังปรับ (แสดงเมื่อกดดูตัวอย่างหรือบันทึก) ----
    if preview_clicked or save_clicked:
        st.subheader("ตัวอย่างผลหลังปรับ")
        adj_mcm_edit = [adj_edit_map[d.date()] * 86400 / 1_000_000 for d in forecast_dates]
        labels = [d.strftime("%d/%m/%Y") for d in forecast_dates]
        preds, err = _preview_model_preds(forecast_dates)
        if preds is None:
            st.info(f"แสดงตัวอย่างผลพยากรณ์ไม่ได้: {err} — แต่ยังบันทึกค่าปรับไว้ล่วงหน้าได้")
            st.dataframe(
                pd.DataFrame({"วันที่": labels, "ปรับเพิ่ม (ลบ.ม./วิ)": list(adj_cms), "เทียบเท่า (ล้าน ลบ.ม./วัน)": [round(a, 3) for a in adj_mcm_edit]}),
                use_container_width=True, hide_index=True,
            )
        else:
            preds_adj, _ = baseflow_module.apply_adjustment(preds, forecast_dates, adj_edit_map)
            prev = pd.DataFrame({
                "วันที่": labels,
                "ผลโมเดล (ล้าน ลบ.ม./วัน)": preds,
                "ปรับเพิ่ม (ลบ.ม./วิ)": list(adj_cms),
                "เทียบเท่า (ล้าน ลบ.ม./วัน)": adj_mcm_edit,
                "หลังปรับ (ล้าน ลบ.ม./วัน)": preds_adj,
            })
            st.line_chart(prev.set_index("วันที่")[["ผลโมเดล (ล้าน ลบ.ม./วัน)", "หลังปรับ (ล้าน ลบ.ม./วัน)"]])
            st.dataframe(prev.round(2), use_container_width=True, hide_index=True)

    # ---- บันทึก (เมื่อกดปุ่มบันทึกเท่านั้น) ----
    if save_clicked:
        try:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            new_rows = pd.DataFrame({
                "date": forecast_dates,
                "adj_cms": list(adj_cms),
                "note": list(notes),
                "updated_by": user,
                "updated_at": now,
            })
            edited_dates = {d.date() for d in forecast_dates}
            frames = []
            if not saved.empty:
                frames.append(saved[~saved["date"].dt.date.isin(edited_dates)])
            nonzero = new_rows[new_rows["adj_cms"].abs() > 1e-9]
            if not nonzero.empty:
                frames.append(nonzero)
            merged = (
                pd.concat(frames, ignore_index=True).sort_values("date")
                if frames else pd.DataFrame(columns=data_io.BASEFLOW_COLS)
            )
            data_io.save_baseflow_adjustments(merged)
            data_io.save_baseflow_audit(new_rows)  # บันทึกทุกวันที่แก้ รวมวันที่ตั้งเป็น 0 ไว้ใน audit
            st.cache_data.clear()
            st.success(f"บันทึกค่าปรับ Baseflow เรียบร้อย (มีผลทันทีที่หน้าพยากรณ์และกราฟสรุป) — {len(nonzero)} วันที่มีค่าปรับ")
        except Exception as e:
            st.error(f"บันทึกไม่สำเร็จ: {e}")

    st.divider()
    with st.expander("📜 ประวัติการแก้ไข (ใครปรับอะไรเมื่อไหร่)"):
        audit = data_io.load_baseflow_audit()
        if audit.empty:
            st.info("ยังไม่มีประวัติ")
        else:
            show = audit.rename(columns={
                "date": "วันที่ที่ปรับ", "adj_cms": "ปรับ (ลบ.ม./วิ)", "note": "หมายเหตุ",
                "updated_by": "ผู้แก้ไข", "updated_at": "เวลาที่บันทึก",
            })
            st.dataframe(show, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
st.title("🌊 ระบบพยากรณ์น้ำไหลเข้าอ่างเก็บน้ำเขื่อนสิริกิติ์")

if data_io.storage_mode() == "local":
    st.sidebar.warning("⚠️ กำลังใช้ local storage (ยังไม่ได้ตั้งค่า Google Drive) — ข้อมูลจะหายถ้า deploy ใหม่")
else:
    st.sidebar.success("✅ เชื่อมต่อ Google Drive แล้ว")

render_login_box()

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📈 พยากรณ์", "📤 อัปโหลดข้อมูลใหม่", "🗂️ ประวัติ", "📊 กราฟสรุป", "🔧 หลังบ้าน (Baseflow)"])
with tab1:
    page_forecast()
with tab2:
    page_upload()
with tab3:
    page_history()
with tab4:
    page_dashboard()
with tab5:
    page_admin()
