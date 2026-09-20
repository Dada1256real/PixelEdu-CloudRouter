from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3
import uuid
import datetime
import json
import httpx
import random

app = FastAPI(title="PixelEdu Enterprise Cloud Router", version="3.0")

ARKESEL_API_KEY = "UUhadk5IS1R5UUp3bk1wdWxoaXg"
ARKESEL_SENDER_ID = "PIXELEDU"
ARKESEL_API_URL = "https://sms.arkesel.com/api/v2/sms/send"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    conn = sqlite3.connect("pixeledu_cloud_staging.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS Cloud_Admissions (Application_ID TEXT PRIMARY KEY, School_ID TEXT, Branch_ID TEXT, Applicant_Name TEXT, Applied_Class TEXT, Parent_Phone TEXT, Parent_Email TEXT, Address TEXT, Previous_School TEXT, Status TEXT, Timestamp TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS Cloud_Parent_Requests (Request_ID TEXT PRIMARY KEY, School_ID TEXT, Student_ID TEXT, Category TEXT, Payload_JSON TEXT, Status TEXT, Timestamp TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS Cloud_Student_Snapshots (School_ID TEXT PRIMARY KEY, Encrypted_JSON_Payload TEXT, Last_Synced TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS Cloud_OTP_Verification (Phone TEXT PRIMARY KEY, OTP_Code TEXT, Expires_At TEXT)''')
    conn.commit()
    conn.close()

init_db()

class AdmissionPayload(BaseModel):
    schoolId: str
    branchId: str
    applicantName: str
    appliedClassId: str
    parentPhone: str
    parentEmail: Optional[str] = ""
    address: str
    previousSchool: Optional[str] = "N/A"

class ParentRequestPayload(BaseModel):
    schoolId: str
    studentId: str
    category: str
    payloadData: dict

class SyncSnapshotPayload(BaseModel):
    schoolId: str
    encryptedPayload: str

class OTPRequestPayload(BaseModel):
    schoolId: str
    studentName: str
    phone: str

class OTPVerifyPayload(BaseModel):
    schoolId: str
    phoneIdentifier: str
    otp: str

@app.post("/api/v1/public/admissions/submit")
async def submit_admission(payload: AdmissionPayload):
    conn = get_db()
    cursor = conn.cursor()
    app_id = f"APP-{str(uuid.uuid4())[:8].upper()}"
    ts = datetime.datetime.utcnow().isoformat()
    try:
        cursor.execute("INSERT INTO Cloud_Admissions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_DOWNLOAD', ?)", 
            (app_id, payload.schoolId, payload.branchId, payload.applicantName, payload.appliedClassId, payload.parentPhone, payload.parentEmail, payload.address, payload.previousSchool, ts))
        conn.commit()
        return {"success": True, "applicationId": app_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/api/v1/public/school-snapshot/{school_id}")
async def get_school_snapshot(school_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT Encrypted_JSON_Payload FROM Cloud_Student_Snapshots WHERE School_ID = ?", (school_id,))
    row = cursor.fetchone()
    conn.close()
    if row: return {"success": True, "data": row["Encrypted_JSON_Payload"]}
    return {"success": False, "error": "School data offline."}

# =========================================================
# 🟢 PRO FIX: BULLETPROOF OTP PRE-VALIDATOR ENGINE
# =========================================================
@app.post("/api/v1/public/parents/request-otp")
async def request_parent_otp(payload: OTPRequestPayload):
    # 1. Clean the input phone number
    digits_only = "".join(filter(str.isdigit, payload.phone))
    clean_phone = "233" + digits_only[1:] if digits_only.startswith("0") and len(digits_only) == 10 else digits_only
    search_name = payload.studentName.strip().lower()

    # 2. Fetch Cloud Snapshot
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT Encrypted_JSON_Payload FROM Cloud_Student_Snapshots WHERE School_ID = ?", (payload.schoolId,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        raise HTTPException(status_code=400, detail="Database Sync in progress. Try again in 30 seconds.")

    try:
        snapshot = json.loads(row["Encrypted_JSON_Payload"])
        students = snapshot.get("Students", [])
        guardians = snapshot.get("Guardians", [])
        
        valid_user = False
        
        # 3. Check Students Table (Fuzzy Matching)
        for stu in students:
            # Strip EVERYTHING except digits from the database phone string
            raw_stu_phone = str(stu.get("Phone", "") or "")
            stu_phone = "".join(filter(str.isdigit, raw_stu_phone))
            if stu_phone.startswith("0") and len(stu_phone) == 10: stu_phone = "233" + stu_phone[1:]
            
            # Split the full name into parts to check if the parent typed just the first or last name
            stu_name_parts = str(stu.get("Full_Name", "") or "").lower().split()
            
            if stu_phone == clean_phone and search_name in stu_name_parts:
                valid_user = True
                break
                
        # 4. Check Guardians Table if not found in Students
        if not valid_user:
            for grd in guardians:
                raw_grd_phone = str(grd.get("Phone_1", "") or "")
                grd_phone = "".join(filter(str.isdigit, raw_grd_phone))
                if grd_phone.startswith("0") and len(grd_phone) == 10: grd_phone = "233" + grd_phone[1:]
                
                if grd_phone == clean_phone:
                    linked_stu_id = grd.get("Student_ID")
                    for stu in students:
                        stu_name_parts = str(stu.get("Full_Name", "") or "").lower().split()
                        if stu.get("Student_ID") == linked_stu_id and search_name in stu_name_parts:
                            valid_user = True
                            break
                if valid_user: break

        if not valid_user:
            conn.close()
            raise HTTPException(status_code=400, detail="Access Denied: No student records match this Name and Phone Number.")
            
    except Exception as e:
        conn.close()
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail="Cloud verification engine error.")

    # 5. Passed Validation! Generate & Send OTP
    otp_code = str(random.randint(1000, 9999))
    expires_at = (datetime.datetime.utcnow() + datetime.timedelta(minutes=10)).isoformat()
    
    try:
        cursor.execute("INSERT INTO Cloud_OTP_Verification (Phone, OTP_Code, Expires_At) VALUES (?, ?, ?) ON CONFLICT(Phone) DO UPDATE SET OTP_Code=excluded.OTP_Code, Expires_At=excluded.Expires_At", (clean_phone, otp_code, expires_at))
        conn.commit()
    finally:
        conn.close()

    sms_payload = { "sender": ARKESEL_SENDER_ID, "message": f"PixelEdu Security: {otp_code} is your Parent Portal OTP. Valid for 10 mins.", "recipients": [clean_phone] }
    
    try:
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(ARKESEL_API_URL, headers={"api-key": ARKESEL_API_KEY, "Content-Type": "application/json"}, json=sms_payload, timeout=10.0)
            if response.status_code in [200, 201]: return {"success": True, "message": "OTP Dispatched."}
            else: raise HTTPException(status_code=500, detail=f"Arkesel Error: {response.text}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/public/parents/verify-otp")
async def verify_parent_otp(payload: OTPVerifyPayload):
    digits_only = "".join(filter(str.isdigit, payload.phoneIdentifier))
    clean_phone = "233" + digits_only[1:] if digits_only.startswith("0") and len(digits_only) == 10 else digits_only

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT OTP_Code, Expires_At FROM Cloud_OTP_Verification WHERE Phone = ?", (clean_phone,))
    row = cursor.fetchone()
    conn.close()
    
    if not row: return {"success": False, "error": "No OTP found for this number."}
    if datetime.datetime.utcnow() > datetime.datetime.fromisoformat(row["Expires_At"]): return {"success": False, "error": "OTP has expired."}
    if row["OTP_Code"] != payload.otp.strip(): return {"success": False, "error": "Invalid PIN code."}
        
    return {"success": True, "token": f"SESSION-{uuid.uuid4().hex[:12].upper()}"}

@app.post("/api/v1/public/parents/submit-request")
async def submit_parent_request(payload: ParentRequestPayload):
    conn = get_db()
    cursor = conn.cursor()
    req_id = f"REQ-{str(uuid.uuid4())[:8].upper()}"
    ts = datetime.datetime.utcnow().isoformat()
    try:
        # 🟢 PRO FIX: Explicit return statement added so the browser receives the 'success' signal!
        cursor.execute("INSERT INTO Cloud_Parent_Requests VALUES (?, ?, ?, ?, ?, 'PENDING_DOWNLOAD', ?)", 
                       (req_id, payload.schoolId, payload.studentId, payload.category, json.dumps(payload.payloadData), ts))
        conn.commit()
        return {"success": True, "requestId": req_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/api/v1/sync/pull-pending/{school_id}")
async def pull_pending_cloud_data(school_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM Cloud_Admissions WHERE School_ID = ? AND Status = 'PENDING_DOWNLOAD'", (school_id,))
    admissions = [dict(row) for row in cursor.fetchall()]
    for app_row in admissions: cursor.execute("UPDATE Cloud_Admissions SET Status = 'SYNCED' WHERE Application_ID = ?", (app_row["Application_ID"],))
        
    cursor.execute("SELECT * FROM Cloud_Parent_Requests WHERE School_ID = ? AND Status = 'PENDING_DOWNLOAD'", (school_id,))
    parent_requests = [dict(row) for row in cursor.fetchall()]
    for req_row in parent_requests: cursor.execute("UPDATE Cloud_Parent_Requests SET Status = 'SYNCED' WHERE Request_ID = ?", (req_row["Request_ID"],))
    
    conn.commit()
    conn.close()
    return { "success": True, "pendingAdmissions": admissions, "pendingParentRequests": parent_requests }

@app.post("/api/v1/sync/push-snapshot")
async def push_student_snapshot(payload: SyncSnapshotPayload):
    conn = get_db()
    cursor = conn.cursor()
    ts = datetime.datetime.utcnow().isoformat()
    try:
        cursor.execute("INSERT INTO Cloud_Student_Snapshots VALUES (?, ?, ?) ON CONFLICT(School_ID) DO UPDATE SET Encrypted_JSON_Payload=excluded.Encrypted_JSON_Payload, Last_Synced=excluded.Last_Synced", (payload.schoolId, payload.encryptedPayload, ts))
        conn.commit()
        return {"success": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)