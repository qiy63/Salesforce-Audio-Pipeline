import os
import json
import re
import pandas as pd
from tqdm import tqdm
from simple_salesforce import Salesforce
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
#               CONFIGURATION
# ==========================================
DOWNLOAD_DIR = "C:\AllWorks\Geminion 0.1.1\Geminion 0.1.1\my_tasksV0.4CallAudioPull\downloads"
INPUT_EXCEL_FILE = "Latest Creation by split for Ignore ZSU.xlsx" 
CASE_ID_COLUMN_NAME = "CASE_ID"
AUTH_STATE_FILE = "sf_auth.json"
OUTPUT_EXCEL_FILE = "Call_Audio_Audit_Report.xlsx"
MAX_WORKERS = 10 # Number of parallel threads to speed up API checking

# ==========================================
#               HELPER FUNCTIONS
# ==========================================
def get_session_from_auth_file():
    if not os.path.exists(AUTH_STATE_FILE):
        raise FileNotFoundError(f"❌ '{AUTH_STATE_FILE}' not found! Run 'a.py' first.")

    with open(AUTH_STATE_FILE, "r") as f:
        auth_data = json.load(f)

    session_id = None
    instance_domain = "zebra.my.salesforce.com" 

    for cookie in auth_data.get("cookies", []):
        if cookie["name"] == "sid" and ("my.salesforce.com" in cookie["domain"] or cookie["domain"] in ["salesforce.com", ".salesforce.com"]):
            if "lightning" not in cookie["domain"]: 
                session_id = cookie["value"]
                break

    if not session_id:
        for cookie in auth_data.get("cookies", []):
            if cookie["name"] == "sid":
                session_id = cookie["value"]
                break

    if not session_id:
        raise ValueError("❌ Failed to find active session ID.")

    return session_id, instance_domain

# ==========================================
#               CORE PROCESSOR
# ==========================================
def check_case(case_id, sf, downloaded_files):
    """Queries Salesforce for a case and cross-references local files."""
    rows = []
    
    try:
        # --- 1. Get Internal Case ID ---
        case_query = f"SELECT Id FROM Case WHERE CaseNumber = '{case_id}'"
        case_result = sf.query(case_query)
        
        if not case_result.get('records'):
            padded_case = str(case_id).zfill(8)
            case_result = sf.query(f"SELECT Id FROM Case WHERE CaseNumber = '{padded_case}'")
            if not case_result.get('records'):
                return [{"CASE_ID": case_id, "FILENAME": "INVALID CASE ID", "Exist?": "N/A", "DateTime": "N/A", "Call Sort": "N/A", "All Exist?": "N/A"}]
                
        real_case_id = case_result['records'][0]['Id']

        # --- 2. Query Voice Calls ---
        vc_query = f"""
            SELECT Id, CallStartDateTime, CallType 
            FROM VoiceCall 
            WHERE RelatedRecordId = '{real_case_id}' 
               OR Case__c = '{real_case_id}'
               OR NF_Case_Number__c = '{real_case_id}'
        """
        try:
            vc_result = sf.query(vc_query)
        except Exception:
            vc_fallback = f"SELECT Id, CallStartDateTime, CallType FROM VoiceCall WHERE RelatedRecordId = '{real_case_id}'"
            vc_result = sf.query(vc_fallback)

        vc_records = vc_result.get('records', [])

        # --- 3. Task Fallback ---
        if not vc_records:
            task_query = f"SELECT Id, Description, CallObject FROM Task WHERE WhatId = '{real_case_id}' AND TaskSubtype = 'Call'"
            try:
                task_result = sf.query(task_query)
                vc_ids = []
                for t in task_result.get('records', []):
                    if t.get('CallObject') and str(t['CallObject']).startswith('0LQ'):
                        vc_ids.append(t['CallObject'])
                    elif t.get('Description'):
                        match = re.search(r'0LQ[a-zA-Z0-9]{15}', t['Description'])
                        if match: vc_ids.append(match.group(0))

                if vc_ids:
                    formatted_ids = "', '".join(vc_ids)
                    vc_task_query = f"SELECT Id, CallStartDateTime, CallType FROM VoiceCall WHERE Id IN ('{formatted_ids}')"
                    vc_result = sf.query(vc_task_query)
                    vc_records = vc_result.get('records', [])
            except Exception:
                pass

        if not vc_records:
            return [{"CASE_ID": case_id, "FILENAME": "NO VOICE CALLS IN SALESFORCE", "Exist?": "N/A", "DateTime": "N/A", "Call Sort": "N/A", "All Exist?": "N/A"}]

        # --- 4. Process and Sort Calls by Date ---
        # Sort the records chronologically by CallStartDateTime
        # (Handling None types gracefully by pushing them to the bottom)
        vc_records.sort(key=lambda x: x.get('CallStartDateTime') or "9999-99-99")

        case_rows = []
        
        for index, vc in enumerate(vc_records):
            vc_id = vc['Id']
            
            call_type = vc['CallType'] if vc.get('CallType') else "UnknownType"
            raw_date = vc['CallStartDateTime']
            call_date = raw_date.split('.')[0].replace('T', ' ').replace(':', '.') if raw_date else "UnknownDate"
            
            # Construct the expected filename
            expected_filename = f"{case_id}_{vc_id}_{call_date}_{call_type}.mp3"
            
            # Check if it actually exists in the Downloads folder
            # We use a flexible check just in case legacy files still exist
            exists = "No"
            for f in downloaded_files:
                if f"{case_id}_{vc_id}" in f and f.endswith(".mp3"):
                    exists = "Yes"
                    expected_filename = f # Update to the actual filename found
                    break
            
            case_rows.append({
                "CASE_ID": case_id,
                "FILENAME": expected_filename,
                "Exist?": exists,
                "DateTime": raw_date if raw_date else "No Date",
                "Call Sort": index + 1 # 1, 2, 3, etc.
            })

        # --- 5. Determine "All Exist?" for the entire Case ---
        all_exist = "Yes" if all(row["Exist?"] == "Yes" for row in case_rows) else "No"
        
        for row in case_rows:
            row["All Exist?"] = all_exist
            rows.append(row)

    except Exception as e:
        rows.append({"CASE_ID": case_id, "FILENAME": f"API ERROR: {e}", "Exist?": "Error", "DateTime": "Error", "Call Sort": "Error", "All Exist?": "Error"})
        
    return rows

# ==========================================
#              MAIN ORCHESTRATOR
# ==========================================
def main():
    print("📂 Scanning local downloads folder...")
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)
    downloaded_files = set(os.listdir(DOWNLOAD_DIR))

    print(f"📊 Loading Excel file: {INPUT_EXCEL_FILE}")
    try:
        df_input = pd.read_excel(INPUT_EXCEL_FILE)
        all_cases = df_input[CASE_ID_COLUMN_NAME].dropna().astype(str).tolist()
        CASE_IDS = [c.replace('.0', '') for c in all_cases] 
    except Exception as e:
        print(f"❌ Error loading Excel: {e}")
        return
        
    print("\n🔑 Authenticating with Salesforce REST API...")
    try:
        session_id, instance_url = get_session_from_auth_file()
        sf = Salesforce(instance=instance_url, session_id=session_id)
        print("✅ REST API Connected Successfully!")
    except Exception as e:
        print(f"❌ REST API Connection Failed: {e}")
        return

    print(f"\n⚡ Commencing Audit for {len(CASE_IDS)} Cases...")
    
    final_report_data = []

    # Run the API checks in parallel for maximum speed
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_case, case_id, sf, downloaded_files): case_id for case_id in CASE_IDS}
        
        for future in tqdm(as_completed(futures), total=len(futures), desc="Auditing Cases"):
            try:
                rows = future.result()
                final_report_data.extend(rows)
            except Exception as e:
                pass

    # Save to Excel
    print("\n📝 Generating Excel Report...")
    report_df = pd.DataFrame(final_report_data)
    
    # Sort the final Excel sheet neatly
    report_df.sort_values(by=["CASE_ID", "Call Sort"], inplace=True)
    
    report_df.to_excel(OUTPUT_EXCEL_FILE, index=False)
    print(f"🎉 Audit Complete! Check your folder for: {OUTPUT_EXCEL_FILE}")

if __name__ == "__main__":
    main()
