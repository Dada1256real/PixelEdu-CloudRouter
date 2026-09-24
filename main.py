from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3
import uuid
import datetime
import json
import httpx
import random
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = FastAPI(title="PixelEdu Enterprise Cloud Router", version="3.1")

ARKESEL_API_KEY = os.getenv("ARKESEL_API_KEY", "UUhadk5IS1R5UUp3bk1wdWxoaXg")
ARKESEL_SENDER_ID = "PIXELEDU"
ARKESEL_API_URL = "https://sms.arkesel.com/api/v2/sms/send"

SMTP_USER = "pixelenxitconsult@gmail.com"
SMTP_PASS = "gnupjqqhbwkpoeas"

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
    cursor.execute('''CREATE TABLE IF NOT EXISTS Cloud_License_Queue (Installation_ID TEXT PRIMARY KEY, School_Name TEXT, Branch_ID TEXT, Amount_Paid REAL, Requested_Days INTEGER, Payment_Method TEXT, Reference_No TEXT, Notes TEXT, Status TEXT, Generated_Key TEXT, Timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

# =========================================================
# 🟢 THREAD-SAFE BACKGROUND WORKERS
# =========================================================
def send_cloud_email_sync(to_email: str, subject: str, html_content: str, school_name: str):
    try:
        msg = MIMEMultipart()
        msg['From'] = f"{school_name} <{SMTP_USER}>"
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(html_content, 'html'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"Cloud Email Error: {e}")

def send_cloud_sms_sync(phone: str, msg: str):
    try:
        sms_payload = { "sender": ARKESEL_SENDER_ID, "message": msg, "recipients": [phone] }
        # Using synchronous HTTPX client so it plays nicely with FastAPI Thread Pools
        with httpx.Client() as client:
            client.post(ARKESEL_API_URL, headers={"api-key": ARKESEL_API_KEY, "Content-Type": "application/json"}, json=sms_payload, timeout=10.0)
    except Exception as e:
        print(f"Cloud SMS Error: {e}")

# =========================================================
# 🟢 PYDANTIC DATA MODELS
# =========================================================
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

class RenewalPayload(BaseModel):
    installationId: str
    schoolName: str
    branchId: str
    amountPaid: float
    requestedDays: int
    paymentMethod: str
    referenceNo: str
    notes: str

class AdminApproveKeyPayload(BaseModel):
    installationId: str
    generatedKey: str

class TeacherAuthPayload(BaseModel):
    schoolId: str
    staffId: str
    pin: str

class TeacherSubmitPayload(BaseModel):
    schoolId: str
    staffId: str
    actionType: str 
    payloadData: dict


# =========================================================
# 🟢 PUBLIC ADMISSIONS (PRO BACKGROUND DISPATCH)
# =========================================================
@app.post("/api/v1/public/admissions/submit")
async def submit_admission(payload: AdmissionPayload, background_tasks: BackgroundTasks):
    conn = get_db()
    cursor = conn.cursor()
    app_id = f"APP-{str(uuid.uuid4())[:8].upper()}"
    ts = datetime.datetime.utcnow().isoformat()
    try:
        cursor.execute("INSERT INTO Cloud_Admissions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_DOWNLOAD', ?)", 
            (app_id, payload.schoolId, payload.branchId, payload.applicantName, payload.appliedClassId, payload.parentPhone, payload.parentEmail, payload.address, payload.previousSchool, ts))
        conn.commit()

        # Generate Safe Variables
        school_name = payload.schoolId.replace("-", " ")
        first_name = payload.applicantName.split(' ')[0]
        admin_portal_link = f"https://admissionpixeledu.netlify.app/?school={payload.schoolId}"

        digits_only = "".join(filter(str.isdigit, payload.parentPhone))
        clean_phone = "233" + digits_only[1:] if digits_only.startswith("0") and len(digits_only) == 10 else digits_only
        
        # 1. Dispatch SMS to Background Thread
        if len(clean_phone) >= 9:
            sms_msg = f"Dear Parent, your application for {first_name} has been received by {school_name}. Ref: {app_id}. Track status here: {admin_portal_link}"
            background_tasks.add_task(send_cloud_sms_sync, clean_phone, sms_msg)

        # 2. Dispatch Email to Background Thread
        if payload.parentEmail and "@" in payload.parentEmail:
            email_html = f"""
            <div style='font-family: "Segoe UI", Arial, sans-serif; max-width: 600px; margin: 0 auto; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; background-color: #ffffff; box-shadow: 0 4px 6px rgba(0,0,0,0.05);'>
                <div style='background-color: #0f172a; padding: 25px; text-align: center; border-bottom: 4px solid #4f46e5;'>
                    <h2 style='color: #ffffff; margin: 0; font-size: 22px; letter-spacing: 2px; text-transform: uppercase;'>{school_name}</h2>
                </div>
                <div style='padding: 35px; color: #334155; line-height: 1.7; font-size: 15px;'>
                    <h3 style='color: #4f46e5; margin-top: 0; border-bottom: 2px solid #f1f5f9; padding-bottom: 12px; font-size: 18px; text-align: center;'>ADMISSION RECEIVED</h3>
                    <p>Dear Parent/Guardian,</p>
                    <p>We have successfully received the admission application for <strong>{payload.applicantName}</strong>.</p>
                    <div style='background-color: #f8fafc; padding: 15px; border-radius: 8px; margin: 20px 0; border: 1px solid #e2e8f0;'>
                        <p style='margin: 0 0 10px 0;'><strong>Reference ID:</strong> {app_id}</p>
                        <p style='margin: 0;'><strong>Status:</strong> <span style='color: #d97706; font-weight: bold;'>Pending Review</span></p>
                    </div>
                    <p>Our admissions committee will review the profile and communicate the next steps.</p>
                    <div style='text-align: center; margin: 30px 0;'>
                        <a href='{admin_portal_link}' style='background-color: #4f46e5; color: #ffffff; padding: 14px 28px; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 14px; display: inline-block;'>Track Application Status</a>
                    </div>
                </div>
            </div>
            """
            background_tasks.add_task(send_cloud_email_sync, payload.parentEmail, f"Application Received - {payload.applicantName}", email_html, school_name)

        return {"success": True, "applicationId": app_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# =========================================================
# 🟢 PUBLIC PARENT PORTAL & OTP ENGINE
# =========================================================
@app.get("/api/v1/public/school-snapshot/{school_id}")
async def get_school_snapshot(school_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT Encrypted_JSON_Payload FROM Cloud_Student_Snapshots WHERE School_ID = ?", (school_id,))
    row = cursor.fetchone()
    conn.close()
    if row: return {"success": True, "data": row["Encrypted_JSON_Payload"]}
    return {"success": False, "error": "School data offline."}

@app.post("/api/v1/public/parents/request-otp")
async def request_parent_otp(payload: OTPRequestPayload, background_tasks: BackgroundTasks):
    digits_only = "".join(filter(str.isdigit, payload.phone))
    clean_phone = "233" + digits_only[1:] if digits_only.startswith("0") and len(digits_only) == 10 else digits_only
    search_name = payload.studentName.strip().lower()

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
        
        for stu in students:
            raw_stu_phone = str(stu.get("Phone", "") or "")
            stu_phone = "".join(filter(str.isdigit, raw_stu_phone))
            if stu_phone.startswith("0") and len(stu_phone) == 10: stu_phone = "233" + stu_phone[1:]
            stu_name_parts = str(stu.get("Full_Name", "") or "").lower().split()
            
            if stu_phone == clean_phone and search_name in stu_name_parts:
                valid_user = True
                break
                
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

    otp_code = str(random.randint(1000, 9999))
    expires_at = (datetime.datetime.utcnow() + datetime.timedelta(minutes=10)).isoformat()
    
    try:
        cursor.execute("INSERT INTO Cloud_OTP_Verification (Phone, OTP_Code, Expires_At) VALUES (?, ?, ?) ON CONFLICT(Phone) DO UPDATE SET OTP_Code=excluded.OTP_Code, Expires_At=excluded.Expires_At", (clean_phone, otp_code, expires_at))
        conn.commit()
    finally:
        conn.close()

    sms_msg = f"PixelEdu Security: {otp_code} is your Parent Portal OTP. Valid for 10 mins."
    background_tasks.add_task(send_cloud_sms_sync, clean_phone, sms_msg)
    
    return {"success": True, "message": "OTP Dispatched."}

@app.post("/api/v1/public/parents/verify-otp")
async def verify_parent_otp(payload: OTPVerifyPayload):
    digits_only = "".join(filter(str.isdigit, payload.phoneIdentifier))
    clean_phone = "233" + digits_only[1:] if digits_only.startswith("0") and len(digits_only) == 10 else digits_only

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT OTP_Code, Expires_At FROM Cloud_OTP_Verification WHERE Phone = ?", (clean_phone,))
    row = cursor.fetchone()
    
    if not row: 
        conn.close()
        return {"success": False, "error": "No OTP found for this number."}
        
    if datetime.datetime.utcnow() > datetime.datetime.fromisoformat(row["Expires_At"]): 
        conn.close()
        return {"success": False, "error": "OTP has expired."}
        
    if row["OTP_Code"] != payload.otp.strip(): 
        conn.close()
        return {"success": False, "error": "Invalid PIN code."}
        
    cursor.execute("DELETE FROM Cloud_OTP_Verification WHERE Phone = ?", (clean_phone,))
    conn.commit()
    conn.close()
        
    return {"success": True, "token": f"SESSION-{uuid.uuid4().hex[:12].upper()}"}

@app.post("/api/v1/public/parents/submit-request")
async def submit_parent_request(payload: ParentRequestPayload):
    conn = get_db()
    cursor = conn.cursor()
    req_id = f"REQ-{str(uuid.uuid4())[:8].upper()}"
    ts = datetime.datetime.utcnow().isoformat()
    try:
        cursor.execute("INSERT INTO Cloud_Parent_Requests VALUES (?, ?, ?, ?, ?, 'PENDING_DOWNLOAD', ?)", 
                       (req_id, payload.schoolId, payload.studentId, payload.category, json.dumps(payload.payloadData), ts))
        conn.commit()
        return {"success": True, "requestId": req_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# =========================================================
# 🟢 TEACHER PORTAL & WORKSPACE API
# =========================================================

@app.post("/api/v1/teacher/auth")
async def teacher_auth(payload: TeacherAuthPayload):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT Encrypted_JSON_Payload FROM Cloud_Student_Snapshots WHERE School_ID = ?", (payload.schoolId,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return {"success": False, "error": "School cloud database is currently offline or syncing."}

    try:
        snapshot = json.loads(row["Encrypted_JSON_Payload"])
        staff_list = snapshot.get("Staff", [])
        
        for staff in staff_list:
            s_id = str(staff.get("Staff_ID", "") or "")
            s_pin = str(staff.get("Academic_PIN", "") or "")

            if s_id.strip().upper() == payload.staffId.strip().upper() and s_pin == payload.pin:
                return {
                    "success": True,
                    "token": f"TCH-{uuid.uuid4().hex[:12].upper()}",
                    "user": {
                        "id": s_id,
                        "fullName": staff.get("Full_Name", "Teacher"),
                        "assignedClasses": json.loads(staff.get("Assigned_Classes", "[]") or "[]"),
                        "assignedSubjects": json.loads(staff.get("Assigned_Subjects", "[]") or "[]")
                    }
                }
                
        return {"success": False, "error": "Authentication Failed. Invalid Staff ID or Academic PIN."}
    except Exception as e:
        return {"success": False, "error": f"Cloud verification error: {str(e)}"}

@app.get("/api/v1/teacher/data/{school_id}")
async def get_teacher_data(school_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT Encrypted_JSON_Payload FROM Cloud_Student_Snapshots WHERE School_ID = ?", (school_id,))
    row = cursor.fetchone()
    conn.close()

    if not row: return {"success": False, "error": "No data found."}

    snapshot = json.loads(row["Encrypted_JSON_Payload"])
    return {
        "success": True,
        "data": {
            "students": snapshot.get("Students", []),
            "classes": snapshot.get("Classes", []),
            "subjects": snapshot.get("Subjects", [])
        }
    }

@app.post("/api/v1/teacher/submit")
async def teacher_submit(payload: TeacherSubmitPayload):
    conn = get_db()
    cursor = conn.cursor()
    req_id = f"TCH-{str(uuid.uuid4())[:8].upper()}"
    ts = datetime.datetime.utcnow().isoformat()
    try:
        cursor.execute("INSERT INTO Cloud_Parent_Requests VALUES (?, ?, ?, ?, ?, 'PENDING_DOWNLOAD', ?)", 
                       (req_id, payload.schoolId, payload.staffId, f"Teacher_{payload.actionType}", json.dumps(payload.payloadData), ts))
        conn.commit()
        return {"success": True, "requestId": req_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# =========================================================
# 🟢 CLOUD SYNCHRONIZATION DAEMON (DESKTOP BRIDGE)
# =========================================================
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


# =========================================================
# 🟢 ENTERPRISE LICENSE EXCHANGE ENGINE
# =========================================================
@app.post("/api/sync/submit-renewal")
async def submit_renewal(payload: RenewalPayload, background_tasks: BackgroundTasks):
    conn = get_db()
    cursor = conn.cursor()
    ts = datetime.datetime.utcnow().isoformat()
    try:
        cursor.execute("INSERT INTO Cloud_License_Queue VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_ADMIN', '', ?) ON CONFLICT(Installation_ID) DO UPDATE SET Amount_Paid=excluded.Amount_Paid, Requested_Days=excluded.Requested_Days, Status='PENDING_ADMIN', Generated_Key='', Timestamp=excluded.Timestamp", 
            (payload.installationId, payload.schoolName, payload.branchId, payload.amountPaid, payload.requestedDays, payload.paymentMethod, payload.referenceNo, payload.notes, ts))
        conn.commit()
        
        vendor_msg = f"[PIXELEDU ALERT] {payload.schoolName} ({payload.branchId}) requested a {payload.requestedDays}-day renewal. Amount: GHS {payload.amountPaid} via {payload.paymentMethod}. Log into Admin Authority to process."
        background_tasks.add_task(send_cloud_sms_sync, "0554794797", vendor_msg)

        return {"success": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/api/sync/check-key/{install_id}")
async def check_approved_key(install_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM Cloud_License_Queue WHERE Installation_ID = ? AND Status = 'APPROVED'", (install_id,))
    row = cursor.fetchone()
    
    if row:
        cursor.execute("UPDATE Cloud_License_Queue SET Status = 'CONSUMED' WHERE Installation_ID = ?", (install_id,))
        conn.commit()
        conn.close()
        return {
            "success": True, "keyWaiting": True, 
            "data": { "generated_key": row["Generated_Key"], "student_count": 1, "additional_days": row["Requested_Days"], "amount_paid": row["Amount_Paid"] }
        }
    
    conn.close()
    return {"success": True, "keyWaiting": False}

@app.get("/api/v1/admin/pending-renewals")
async def admin_pull_renewals():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM Cloud_License_Queue WHERE Status = 'PENDING_ADMIN'")
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"success": True, "data": rows}

@app.post("/api/v1/admin/approve-key")
async def admin_approve_key(payload: AdminApproveKeyPayload):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE Cloud_License_Queue SET Status = 'APPROVED', Generated_Key = ? WHERE Installation_ID = ?", (payload.generatedKey, payload.installationId))
        conn.commit()
        return {"success": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.delete("/api/v1/admin/delete-request/{install_id}")
async def admin_delete_request(install_id: str):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM Cloud_License_Queue WHERE Installation_ID = ?", (install_id,))
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
