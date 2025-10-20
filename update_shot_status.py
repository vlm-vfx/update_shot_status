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

# --- FMP AUTH ---
def fmp_login():
    url = f"{FMP_SERVER}/fmi/data/v2/databases/{FMP_DB}/sessions"
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

# --- FMP SCRIPT CALL ---
def fmp_update_status(token, sg_id, fmp_status):
    script_name = "SG_update_status"
    url = f"{FMP_SERVER}/fmi/data/v2/databases/{FMP_DB}/scripts/{quote(script_name)}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    param = json.dumps({"SG_ID": sg_id, "Status": fmp_status})
    payload = {"script.param": param}
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        result_json = response.json()
        if "messages" in result_json and any(m.get("code") != "0" for m in result_json["messages"]):
            print(f"❌ FMP script error for SG_ID {sg_id}: {result_json}")
            return False, result_json
        print(f"✅ FMP script success for SG_ID {sg_id}: {result_json}")
        return True, result_json
    except Exception as e:
        print(f"❌ Exception calling FMP script for SG_ID {sg_id}: {e}")
        return False, {"error": str(e)}

# --- FLASK ROUTE ---
@app.route("/update_shot_status", methods=["GET", "POST"])
def update_shot_status():
    # Merge GET, POST, JSON
    data = {}
    data.update(request.args)
    data.update(request.form)
    if request.is_json:
        data.update(request.get_json())

    selected_ids = data.get("selected_ids", "")
    ids = [int(x) for x in selected_ids.split(",") if x.strip().isdigit()]
    
    debug = data.get("debug", "false").lower() == "true"
    log = []

    if not ids:
        return "No valid Version IDs received from SG.", 400

    # --- Get Versions + linked Shots ---
    versions = sg.find(
        "Version",
        [["id", "in", ids]],
        ["id", "code", "entity.Shot.id", "entity.Shot.sg_status_list"]
    )
    
    fmp_token = fmp_login()
    updated = 0
    skipped = 0

    for v in versions:
        shot_id = v.get("entity.Shot.id")
        shot_status = v.get("entity.Shot.sg_status_list")

        log_entry = {
            "version_id": v["id"],
            "shot_id": shot_id,
            "shot_status": shot_status,
        }

        if not shot_id:
            skipped += 1
            log_entry["note"] = "No linked Shot"
            log.append(log_entry)
            continue

        fmp_status = STATUS_MAP.get(shot_status, "Unknown")
        log_entry["mapped_status"] = fmp_status

        if fmp_status == "Unknown":
            skipped += 1
            log_entry["note"] = f"Unmapped status {shot_status}"
            log.append(log_entry)
            continue

        try:
            success, fmp_result = fmp_update_status(fmp_token, shot_id, fmp_status)
            if success:
                updated += 1
            else:
                skipped += 1
                log_entry["note"] = "FMP script failed"
                log_entry["fmp_result"] = fmp_result
        except Exception as e:
            skipped += 1
            log_entry["note"] = f"Exception during FMP call: {e}"

        log.append(log_entry)

    # --- Return JSON ---
    result = {
        "message": f"✅ Updated {updated} shots in FileMaker. Skipped {skipped}.",
        "updated": updated,
        "skipped": skipped,
    }

    if debug:
        result["debug_log"] = log

    return jsonify(result)

# --- RUN APP ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
