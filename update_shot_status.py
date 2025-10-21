from shotgun_api3 import Shotgun
from flask import Flask, request, jsonify
import os
import requests
import json
from urllib.parse import quote
from base64 import b64encode

app = Flask(__name__)

# --- CONFIG ---
SG_URL = os.environ.get("SG_URL")
SG_SCRIPT_NAME = os.environ.get("SG_SCRIPT_NAME")
SG_API_KEY = os.environ.get("SG_API_KEY")

FMP_SERVER = os.environ.get("FMP_SERVER")
FMP_DB = os.environ.get("FMP_DB")
FMP_USERNAME = os.environ.get("FMP_USERNAME")
FMP_PASSWORD = os.environ.get("FMP_PASSWORD")

# --- CONNECT TO SHOTGRID ---
sg = Shotgun(SG_URL, script_name=SG_SCRIPT_NAME, api_key=SG_API_KEY)

# --- STATUS MAP (SG → FMP) ---
STATUS_MAP = {
    "wtg": "NEW",
    "ip": "IN PROGRESS",
    "hld": "ON HOLD",
    "profi": "NEED POST APPROVAL",
    "apr": "APPROVED",
    "omt": "OMIT"
}

def fmp_login():
    """Authenticate and return FMP session token"""
    url = f"{FMP_SERVER}/fmi/data/vLatest/databases/{FMP_DB}/sessions"
    auth_string = f"{FMP_USERNAME}:{FMP_PASSWORD}"
    auth_base64 = b64encode(auth_string.encode("utf-8")).decode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {auth_base64}",
    }
    r = requests.post(url, headers=headers)
    if r.status_code == 200:
        return r.json()["response"]["token"]
    else:
        raise Exception(f"FMP Login failed: {r.text}")

def fmp_update_status(token, sg_id, fmp_status):
    """Update a record in FMP where SG_ID matches"""
    url = f"{FMP_SERVER}/fmi/data/vLatest/databases/{FMP_DB}/layouts/status_update/_find"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    query = {"query": [{"SG_ID": str(sg_id)}]}
    print(f" Querying FMP for SG_ID={sg_id}")
    print(f"   → POST {url}")
    print(f"   → BODY: {json.dumps(query)}")

    find_response = requests.post(url, headers=headers, json=query)
    print(f"   ← RESPONSE {find_response.status_code}: {find_response.text}")

    if find_response.status_code != 200:
        print(f"FMP find failed for SG_ID {sg_id}: {find_response.text}")
        return False

    
    record_id = data[0]["recordId"]
    
    # Update status field
    update_url = f"{FMP_SERVER}/fmi/data/vLatest/databases/{FMP_DB}/layouts/status_update/records/{record_id}"
    update_data = {"fieldData": {"Status": fmp_status}}
    update_response = requests.patch(update_url, headers=headers, json=update_data)
    
    if update_response.status_code == 200:
        print(f" Updated SG_ID {sg_id} → {fmp_status}")
        return True
    else:
        print(f" Failed to update SG_ID {sg_id}: {update_response.text}")
        return False

@app.route("/update_shot_status", methods=["GET", "POST"])
def update_fmp_status():
    data = {}
    data.update(request.args)
    data.update(request.form)
    if request.is_json:
        data.update(request.get_json())

    # --- Enable debug mode via URL param ---
    DEBUG = str(data.get("debug", "false")).lower() in ("1", "true", "yes")

    selected_ids = data.get("selected_ids", "")
    ids = [int(x) for x in selected_ids.split(",") if x.strip().isdigit()]

    if not ids:
        return "No valid Version IDs received from SG.", 400

    print(f"\n Received AMI Request")
    print(f"   → Version IDs: {ids}")
    print(f"   → Debug mode: {DEBUG}")

    # --- Get Versions + linked Shots ---
    versions = sg.find(
    "Version",
    [["id", "in", ids]],
    ["id", "code", "entity", "entity.Shot.code", "entity.Shot.id", "entity.Shot.sg_status_list"]
    )
    
    fmp_token = fmp_login()
    updated = 0
    skipped = 0
    
    for v in versions:
        entity = v.get("entity")

        if not entity or entity.get("type") != "Shot":
            print(f"⚠ Skipping version {v['id']} (no linked Shot or entity type = {entity.get('type') if entity else 'None'})")
            skipped += 1
            continue

        shot = entity

        # For clarity, print what we got from SG
        if DEBUG:
            print(f"\n Version {v['code']} → Linked Shot: {shot}")
        if DEBUG:
            print(f" Linked shot: {shot}")
        
        shot_data = sg.find_one("Shot", [["id", "is", shot["id"]]], ["id", "sg_status_list"])
        if not shot_data:
            skipped += 1
            continue

        sg_id = shot_data["id"]
        sg_status = shot_data.get("sg_status_list")
        fmp_status = STATUS_MAP.get(sg_status, "Unknown")

        if DEBUG:
            print(f"   SG Shot ID: {sg_id}")
            print(f"   SG Status: {sg_status}")
            print(f"   FMP Status: {fmp_status}")

        if fmp_status == "Unknown":
            print(f"Skipping SG_ID {sg_id} with unmapped status {sg_status}")
            skipped += 1
            continue

        success = fmp_update_status(fmp_token, sg_id, fmp_status)
        if success:
            updated += 1

    print(f"\n Finished updating — {updated} updated, {skipped} skipped.")

    return jsonify({
        "message": f" Updated {updated} shots in FileMaker. Skipped {skipped}.",
        "updated": updated,
        "skipped": skipped,
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
