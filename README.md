# Aniimo — UID / Server Character Verification API

RESTful API สำหรับรับ UID และ Server แล้วตรวจข้อมูลตัวละคร พร้อมหลักฐานการวิเคราะห์ Webshop และแนวทางรับมือข้อผิดพลาด

## สถานะงานและข้อจำกัดสำคัญ

**ยังไม่บรรลุการตรวจ UID ของผู้เล่นทั่วไปโดยไม่ล็อกอิน** ห้ามนำผล mock หรือการตรวจเฉพาะบัญชีมาสรุปว่าส่วนนี้สำเร็จแล้ว

| ส่วนงาน | สิ่งที่พิสูจน์ได้ |
|---|---|
| `POST /api/v1/aniimo/verify` | มี implementation และ tests |
| ตัวละครจริงของบัญชีทดสอบ | ผู้ใช้และการเรียก API ในเครื่องยืนยัน Miwx, UID `412001104638`, Server `4`, source `session`, is_mock `false` |
| API ต้นทาง | `GET https://worldx-office-api.aniimo.com/api/player/roles`, Bearer authentication, JSON `code: 200` |
| ขอบเขตของ roles | รายชื่อตัวละครในบัญชีที่ token อ้างถึง ไม่ได้รับ UID/Server ใน query |
| UID นอกบัญชี | ตรวจไม่ได้ผ่าน session provider; ตอบ `UID_NOT_IN_ACCOUNT` ไม่ยืนยันว่าตัวละครไม่มีอยู่ทั้งเกม |
| UID lookup อิสระ | ยังไม่พบใน endpoint maps / login SDK ที่ตรวจ ไม่ใช่ข้อพิสูจน์ว่าไม่มี API อื่น |
| Partner provider | โครงเชื่อมต่อที่ตั้งค่าได้ ยังไม่มี endpoint จริงที่ได้รับสิทธิ์มายืนยัน |
| หน้าเว็บ/สินค้า/ออเดอร์ | งานสาธิตเพิ่มเติม ไม่ใช่หลักฐานว่า UID lookup อิสระสำเร็จ |
| TWD / MyCard | ยังไม่ยืนยันกับต้นทางจริง ไม่ใช่เงื่อนไขก่อนทดสอบ `/verify` |

## การวิเคราะห์ระบบ

ดู [หลักฐานและ endpoint map](docs/VERIFICATION_EVIDENCE.md) และ [บันทึกการวิเคราะห์เดิม](docs/REVERSE_ENGINEERING.md)

ขั้นตอนที่ใช้:

1. อ่าน HTML หน้า Webshop และไฟล์ JavaScript ที่หน้าเว็บอ้างถึง รวมถึง login SDK
2. ตรวจ endpoint map, การประกอบ header/payload และการโหลดรายชื่อตัวละคร
3. เทียบกับ Network capture ของบัญชีทดสอบที่ผู้ใช้ได้รับอนุญาต
4. เรียก roles แบบไม่ส่ง credential หนึ่งครั้ง ได้ HTTP 401
5. ตรวจผ่าน API ในเครื่องที่ผู้ใช้ตั้ง token ไว้ ได้ข้อมูลจริงของบัญชีนั้น โดยไม่พิมพ์ token ลง log
6. ใช้ HTTP mocks สำหรับ error cases และ order tests; ไม่สร้างออเดอร์จริงเพื่อพิสูจน์ UID lookup

ไม่เดาหรือไล่เรียก endpoint ที่ไม่พบในหลักฐาน ไม่ตีความ token ว่าแปลงกลับเป็น UID ได้ และไม่ hard-code ตัวละครจริงเป็นผลตรวจทั่วไป

### Headers, cookies, tokens และ signature

- Office roles: `Authorization: Bearer <operator token>`, `Accept: application/json`; SDK ใช้ `credentials: omit` จึงไม่มีหลักฐานว่า cookie เป็นกลไกยืนยันตัวตนหลักของ endpoint นี้
- ไม่ส่ง token ผ่าน request body ของ API สาธารณะ, `.http`, Postman หรือหน้าเว็บลูกค้า
- Cookie ของ CDN/WAF ไม่ใช่หลักฐานว่ามีสิทธิ์อ่านตัวละคร
- Store อ่านข้อมูลด้วย JSON envelope: `game_id`, `game_project`, `source`, `server_id` ซึ่งต้องเป็น **JSON integer**
- ในเส้นทาง roles และ store ที่ตรวจ ไม่พบ custom HMAC/signature บน request; เป็นข้อสังเกตเฉพาะไฟล์/เส้นทางที่ตรวจ ไม่ได้หมายความว่าทั้งระบบไม่มี cryptography
- Bearer token เป็น opaque credential ไม่ใช่ UID; ขนส่งผ่าน HTTPS ส่วน login มี OTP และกลไก device/risk challenge แยกต่างหาก

## ติดตั้งและรัน (Windows CMD)

```cmd
cd /d D:\claude-workspace\aniimo-uid-topup
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

โหมดจำลอง ไม่เรียกเกมจริง:

```cmd
set ANIIMO_ENV=dev
set ANIIMO_ROLE_PROVIDER=mock
set ANIIMO_IDEMPOTENCY_BACKEND=memory
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

เปิด `/docs` เพื่อทดสอบ API หรือ `/` เพื่อทดลองหน้าเว็บ ใช้ UID `412000126173` / Server `4` สำหรับ fixture

### ตรวจบัญชีทดสอบจริง

```cmd
.venv\Scripts\python.exe -m tools.run_session
```

กรอก token ใหม่ของบัญชีที่ได้รับอนุญาตใน hidden terminal prompt แล้วเปิด `http://127.0.0.1:8002/docs` ไม่ต้องใช้ VPN Taiwan สำหรับการตรวจตัวละครบัญชีเดิม หากต้นทางอนุญาตการเชื่อมต่อปัจจุบัน

โปรแกรมเก็บ credential ใน environment ของ child process ไม่เขียนลงไฟล์หรือ command-line arguments ห้ามส่ง token ในแชตหรือ commit ลง Git

Session provider เรียก roles หนึ่งครั้ง แล้วจับคู่ `roleId == uid` และ `serverArea == server` คืน `roleName`/`nickname`, `level`, `openid`, `avatarUrl` โดยไม่แต่งชื่อเมื่อข้อมูลว่าง

## API specification

```http
POST /api/v1/aniimo/verify
Content-Type: application/json

{"uid":"412001104638","server":"4"}
```

UID ต้องเป็น ASCII digits 4–20 หลัก; Server เป็น ASCII digits 1–6 หลักและค่ามากกว่า 0 รหัส Server ที่มีรูปแบบถูกต้องยังต้องจับคู่กับข้อมูลต้นทาง

ผลจริงของบัญชีที่เชื่อมแล้ว (openid ถูกปกปิด):

```json
{
  "verified": true,
  "character": {
    "uid": "412001104638",
    "server": "4",
    "role_name": "Miwx",
    "level": 0,
    "openid": "<redacted>",
    "avatar_url": null,
    "is_mock": false
  },
  "source": "session",
  "note": "Verified only among the operator account's roles; arbitrary UID lookup is not available through this provider."
}
```

ตัวอย่างละค่า avatar ที่ไม่จำเป็นออก; ค่าจริงอาจมี URL รูป ระดับ 0 คือค่าที่ต้นทางส่งมา ไม่ได้แก้เป็นระดับที่คาดเดา

| HTTP | Code | ความหมาย |
|---|---|---|
| 200 | — | จับคู่ตัวละครได้; ดู `source`, `is_mock` และ `note` ประกอบ |
| 422 | FastAPI validation error | UID/Server รูปแบบผิด |
| 404 | `UID_NOT_FOUND` | ไม่พบในแหล่งข้อมูลที่ provider ตรวจได้ เช่น mock |
| 404 | `UID_NOT_IN_ACCOUNT` | UID/Server ไม่อยู่ในบัญชีที่เชื่อม ไม่สรุปว่าไม่มีในเกม |
| 502 | `ACCOUNT_AUTH_REQUIRED` | credential ของต้นทางถูกปฏิเสธ/หมดอายุ |
| 429 | `RATE_LIMITED` / `RATE_LIMITED_UPSTREAM` | เกินความถี่ พร้อม Retry-After |
| 403 | `IP_BLOCKED` | ต้นทางปฏิเสธ IP ในเส้นทางที่จำแนกได้ |
| 423 | `CAPTCHA_REQUIRED` | ต้นทางร้องขอ challenge ที่ต้องจัดการตาม flow ที่รองรับ |
| 502 | `UPSTREAM_UNAVAILABLE` | timeout, ต้นทางขัดข้อง, response ผิด หรือ circuit เปิด |

App errors ใช้ `{"error":{"code":"...","message":"..."}}`; validation ของ FastAPI ใช้ `{"detail":[...]}`

## Edge cases

ดู [รายละเอียด](docs/EDGE_CASES.md) โดยเฉพาะ:

- token bucket ของ client IP และ circuit breaker ก่อนเรียก verify upstream
- ไม่เชื่อ X-Forwarded-For จาก client โดยตรง; trusted proxy ต้องตั้งค่าเอง
- เมื่อ IP ถูกบล็อกหรือเจอ CAPTCHA ให้หยุด/รอและดำเนินการตามวิธีที่ต้นทางรองรับ ไม่หมุน proxy เพื่อหลบการบล็อก
- ไม่แก้ CAPTCHA อัตโนมัติ ไม่สรุปว่าทุก HTTP 403 เป็น token หมดอายุ
- partner response ต้องไม่คืน UID/Server คนละตัวหรือ business error แล้วรายงาน verified

## ทดสอบและสิ่งที่ต้องส่ง

```cmd
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

- Source: `app/`, `tools/`, `requirements.txt`
- README และหลักฐาน: `docs/VERIFICATION_EVIDENCE.md`, `docs/EDGE_CASES.md`
- ชุดทดสอบ: `tests/`
- [HTTP examples](requests.http) หรือ [Postman collection](docs/postman_collection.json)
- GitHub repository: ยังไม่ได้ยืนยันการเผยแพร่ ต้องตั้ง destination และตรวจ credential ก่อน push

Tests ที่ mock upstream พิสูจน์การจัดการ contract/error เท่านั้น ไม่พิสูจน์ว่า endpoint partner จริงมีอยู่หรือว่าค้นหา UID ทั่วไปได้

**เงื่อนไขที่ต้องได้เพิ่มเพื่อปิด Scope:** endpoint/สิทธิ์ที่อนุญาตให้ค้นหา UID + Server ของผู้เล่นที่ไม่ได้อยู่ในบัญชี operator พร้อมหลักฐาน request/response จริงที่ปกปิด credential แล้ว
