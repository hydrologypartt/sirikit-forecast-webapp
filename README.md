# ระบบพยากรณ์น้ำไหลเข้าอ่างเก็บน้ำเขื่อนสิริกิติ์ (7 วันล่วงหน้า)

## โครงสร้างไฟล์
```
app.py              - แอปหลัก Streamlit (3 แท็บ: พยากรณ์ / อัปโหลดข้อมูล / ประวัติ)
data_io.py          - อ่าน/เขียนข้อมูล รองรับทั้ง local และ Google Drive
features.py         - สร้าง feature (lag, rolling, ฤดูกาล, สถานีน้ำท่า)
model.py            - เทรนและพยากรณ์ (HistGradientBoostingRegressor x7 horizon)
station_parser.py   - parser ไฟล์สถานีน้ำท่ารูปแบบ RID Water Year (.txt)
shp_reader.py       - pure-Python shapefile reader + point-in-polygon (ไม่ต้องใช้ geopandas)
rainfall.py         - clip ฝนพยากรณ์ด้วยขอบเขตพื้นที่รับน้ำ (ยังไม่ได้ต่อเข้า app.py — ดูหัวข้อ "สิ่งที่ยังไม่เสร็จ")
requirements.txt    - ไลบรารีที่ต้องติดตั้ง
```

## รันทดสอบในเครื่องตัวเอง (โหมด local, ไม่ต้องมี Google Drive)
```bash
pip install -r requirements.txt
streamlit run app.py
```
โหมดนี้จะเก็บไฟล์ไว้ในโฟลเดอร์ `./data` แทน Google Drive — เหมาะสำหรับทดสอบว่าหน้าตา/การทำงานถูกต้องก่อน deploy จริง

## ตั้งค่า Google Drive สำหรับเก็บข้อมูลถาวร
1. เปิด https://console.cloud.google.com → สร้างโปรเจกต์ใหม่ (หรือใช้โปรเจกต์เดิม)
2. เปิดใช้งาน **Google Drive API** (เมนู APIs & Services → Enable APIs)
3. สร้าง **Service Account** (APIs & Services → Credentials → Create Credentials → Service Account)
4. สร้างคีย์แบบ **JSON** ให้ service account นั้น แล้วดาวน์โหลดไฟล์ .json มาเก็บไว้
5. เปิด Google Drive ของคุณ สร้างโฟลเดอร์ใหม่ 1 โฟลเดอร์สำหรับเก็บข้อมูลของแอปนี้
6. **แชร์โฟลเดอร์นั้นให้กับอีเมลของ service account** (อีเมลอยู่ในไฟล์ .json ช่อง `client_email`) สิทธิ์ระดับ "แก้ไขได้" (Editor)
7. คัดลอก **Folder ID** จาก URL ของโฟลเดอร์ (ส่วนท้ายของ URL หลัง `/folders/`)

## Deploy บน Streamlit Community Cloud
1. Push โค้ดทั้งหมดนี้ขึ้น GitHub repository
2. เข้า https://share.streamlit.io → New app → เลือก repo นี้ → ไฟล์หลัก `app.py`
3. ไปที่ **App settings → Secrets** ใส่ค่าตามรูปแบบนี้ (เอาข้อมูลจากไฟล์ .json ที่ดาวน์โหลดมาในขั้นตอนก่อนหน้า):
```toml
gdrive_folder_id = "ใส่ Folder ID ที่คัดลอกมา"

[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "...@....iam.gserviceaccount.com"
client_id = "..."
token_uri = "https://oauth2.googleapis.com/token"
```
4. กด Deploy — เมื่อ deploy เสร็จแอปจะได้ URL ถาวร (เช่น `yourapp.streamlit.app`) เปิดได้จากทุกเครื่อง

## สรุปผลโมเดลปัจจุบัน (ทดสอบกับข้อมูลปี 2024 - มี.ค. 2026 ที่ไม่เคยเทรน)
| วันล่วงหน้า | MAE | RMSE | R² |
|---|---|---|---|
| 1 | 4.78 | 12.31 | 0.879 |
| 2 | 5.44 | 14.42 | 0.834 |
| 3 | 7.60 | 19.58 | 0.694 |
| 4 | 9.40 | 22.81 | 0.584 |
| 5 | 10.06 | 24.38 | 0.525 |
| 6 | 10.53 | 25.89 | 0.465 |
| 7 | 11.04 | 26.67 | 0.432 |

โมเดลชนะ naive baseline (ใช้ค่าเมื่อวานซ้ำ) ทุก horizon โดยเฉพาะวันที่ 1-2 ที่ดีขึ้นราว 45% หลังเพิ่มตัวแปรสถานีน้ำท่า (N.1, N.13A, N.64)

## ⚠️ ข้อจำกัดที่ทราบอยู่แล้ว (สำคัญ อ่านก่อนใช้งานจริง)
1. **พยากรณ์ต่ำกว่าจริงในช่วงน้ำมาก/น้ำท่วมฉับพลัน** โดยเฉพาะวันที่ 4-7 — เพราะยังไม่มีข้อมูลฝนพยากรณ์ล่วงหน้าเป็น feature จริง (มีแค่โมดูล `rainfall.py` เตรียมไว้ ยังไม่ได้ต่อเข้า pipeline การเทรน เนื่องจากยังไม่มีข้อมูลฝนย้อนหลัง 20 ปีมาเทรน)
2. **overfitting ในโมเดล horizon 4-7** (R² train ~0.93 แต่ test เหลือ 0.43-0.58) —ยังไม่ได้ปรับ regularization
3. **LSTM ยังไม่ได้ทำ** — ทดลองแล้วว่า HistGradientBoostingRegressor beat naive baseline ได้ดี จึงใช้เป็น baseline หลักก่อน

## สิ่งที่ยังไม่เสร็จ / ทำต่อได้
- ต่อโมดูล `rainfall.py` เข้ากับ `features.py`/`model.py` เป็น feature จริง (ต้องหาข้อมูลฝนย้อนหลังมาเทรนก่อน เช่น Open-Meteo Historical API)
- Quantile regression หรือ weighted loss เพื่อลดปัญหาพยากรณ์ต่ำกว่าจริงช่วงน้ำมาก
- ปรับ regularization ของโมเดล horizon 4-7 ลด overfitting
- หน้าอัปโหลดไฟล์ฝนพยากรณ์ + shapefile ในตัวแอป (ตอนนี้ทำได้แค่ผ่านโค้ดโดยตรง ยังไม่มี UI)
- ระบบแจ้งเตือนอัตโนมัติ (เช่น LINE Notify) เมื่อพยากรณ์เกินเกณฑ์ที่กำหนด
