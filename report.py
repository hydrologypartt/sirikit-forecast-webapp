"""
report.py
สร้างกราฟรายงานสรุป (ภาพ .png) สไตล์คล้ายรายงานพยากรณ์น้ำของกรมชลประทาน:
แท่งฝน (จริง/คาดการณ์) + เส้นปริมาณน้ำในอ่าง (จริง/คาดการณ์) + เส้นความจุเก็บกัก + ตัวเลขกำกับทุกจุด
"""
import io
import os
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.image as mpimg
from matplotlib import font_manager

THAI_FONT_CANDIDATES = [
    "Tahoma", "Leelawadee UI", "TH Sarabun New", "Angsana New",
    "Noto Sans Thai", "Loma", "Norasi",
]

LOGO_PATH = os.path.join(os.path.dirname(__file__), "assets", "rid_logo.png")


def _set_thai_font():
    """ลองหาฟอนต์ที่รองรับภาษาไทยในเครื่อง ถ้าไม่เจอเลยจะใช้ฟอนต์ default
    (ภาษาไทยในภาพอาจกลายเป็นสี่เหลี่ยมถ้าเครื่องไม่มีฟอนต์ไทยติดตั้งไว้)"""
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in THAI_FONT_CANDIDATES:
        if name in available:
            plt.rcParams["font.family"] = name
            return name
    return None


def build_summary_text(dam_name, last_date, last_storage, capacity, forecast_dates,
                        preds, assumed_release, storage_forecast):
    pct_now = last_storage / capacity * 100
    remain_now = capacity - last_storage
    total_inflow = sum(preds)
    total_release = assumed_release * len(preds)
    net = total_inflow - total_release
    end_storage = storage_forecast[-1]
    pct_end = end_storage / capacity * 100
    remain_end = capacity - end_storage

    d0 = last_date.strftime("%d %b %Y")
    d_start = forecast_dates[0].strftime("%d %b")
    d_end = forecast_dates[-1].strftime("%d %b %Y")

    return (
        f"สถานการณ์ปัจจุบัน วันที่ {d0} มีปริมาณน้ำในอ่างฯ {last_storage:,.2f} ล้าน ลบ.ม. "
        f"คิดเป็น {pct_now:.2f}% ของความจุเก็บกัก สามารถรับน้ำได้อีก {remain_now:,.2f} ล้าน ลบ.ม.\n"
        f"คาดการณ์ วันที่ {d_start} – {d_end} จะมีปริมาณน้ำไหลลงอ่างฯ รวม {total_inflow:,.2f} ล้าน ลบ.ม. "
        f"คงการระบายน้ำที่ {assumed_release:,.2f} ล้าน ลบ.ม./วัน จะมีน้ำระบายออกรวม {total_release:,.2f} ล้าน ลบ.ม. "
        f"{'เหลือน้ำสะสม' if net >= 0 else 'ขาดดุลน้ำ'} {abs(net):,.2f} ล้าน ลบ.ม. "
        f"วันที่ {d_end} จะมีปริมาณน้ำในอ่าง {end_storage:,.2f} ล้าน ลบ.ม. "
        f"คิดเป็น {pct_end:.2f}% ของความจุเก็บกัก สามารถรับน้ำได้อีก {remain_end:,.2f} ล้าน ลบ.ม."
    )


def generate_report_image(dam_name, hist, forecast_dates, preds, storage_forecast,
                           capacity, rain_hist, rain_fc, last_date, last_storage):
    """คืนค่า PNG bytes ของกราฟรายงาน"""
    font_used = _set_thai_font()

    fig, ax1 = plt.subplots(figsize=(14, 7), dpi=150)
    ax2 = ax1.twinx()

    if rain_hist is not None and not rain_hist.dropna().empty:
        ax2.bar(rain_hist.index, rain_hist.values, color="#4c78a8", alpha=0.6,
                label="ฝนจริงเฉลี่ย (มม.)", width=0.7)
    if rain_fc is not None and any(v is not None for v in rain_fc):
        vals = [v if v is not None else 0 for v in rain_fc]
        ax2.bar(forecast_dates, vals, color="#f58518", alpha=0.6,
                label="ฝนคาดการณ์เฉลี่ย (มม.)", width=0.7)
    ax2.invert_yaxis()
    ax2.set_ylabel("ปริมาณฝน (มม.)")

    ax1.plot(hist.index, hist["storage"], marker="o", markersize=4, color="#1f77b4",
             label="ปริมาณน้ำในอ่างจริง (ล้าน ลบ.ม.)")
    for x, y in zip(hist.index, hist["storage"]):
        ax1.annotate(f"{y:,.2f}", (x, y), textcoords="offset points", xytext=(0, 8),
                     ha="center", fontsize=6.5, color="#1f77b4", rotation=45)

    fc_x = [last_date] + list(forecast_dates)
    fc_y = [last_storage] + list(storage_forecast)
    ax1.plot(fc_x, fc_y, marker="o", markersize=5, linestyle="--", color="#d62728",
             label="ปริมาณน้ำในอ่างคาดการณ์ (ล้าน ลบ.ม.)")
    for x, y in zip(forecast_dates, storage_forecast):
        ax1.annotate(f"{y:,.2f}", (x, y), textcoords="offset points", xytext=(0, 8),
                     ha="center", fontsize=6.5, color="#d62728", rotation=45)

    ax1.axhline(capacity, color="red", linestyle="--", linewidth=1.2)
    ax1.text(hist.index[min(2, len(hist.index) - 1)], capacity,
             f"ความจุเก็บกัก {capacity:,.2f} ล้าน ลบ.ม.", color="red", fontsize=8, va="bottom")

    ax1.set_ylabel("ปริมาณน้ำในอ่าง (ล้าน ลบ.ม.)")
    ax1.set_ylim(bottom=0)
    ax1.set_title(f"กราฟแสดงปริมาณน้ำในอ่างเก็บน้ำ{dam_name}", fontsize=15, pad=14)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax1.grid(axis="y", linestyle=":", alpha=0.4)
    fig.autofmt_xdate(rotation=45)

    if os.path.exists(LOGO_PATH):
        logo_img = mpimg.imread(LOGO_PATH)
        logo_ax = fig.add_axes([0.01, 0.90, 0.07, 0.09])
        logo_ax.imshow(logo_img)
        logo_ax.axis("off")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper center",
               bbox_to_anchor=(0.5, -0.18), ncol=3, fontsize=8, frameon=False)

    if font_used is None:
        fig.text(0.5, 1.02, "[คำเตือน: ไม่พบฟอนต์ไทยในเครื่อง ตัวอักษรไทยอาจแสดงผลไม่ถูกต้อง]",
                  ha="center", fontsize=8, color="gray")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue(), font_used
