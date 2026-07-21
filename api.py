import os
import json
import re
import time
import asyncio
import requests
import aiofiles
import pandas as pd
from tqdm.asyncio import tqdm
from pydub import AudioSegment
from simple_salesforce import Salesforce
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from concurrent.futures import ProcessPoolExecutor

# ==========================================
#               CONFIGURATION
# ==========================================
SF_DOMAIN = "zebra.lightning.force.com"
DOWNLOAD_DIR = "./downloads"
EXCEL_FILE_PATH = "Latest Creation by split for Ignore ZSU.xlsx" 
CASE_ID_COLUMN_NAME = "CASE_ID"
AUTH_STATE_FILE = "sf_auth.json"
SAVEPOINT_FILE = "savepoint.json" 
MAX_CONCURRENT_TABS = 4  
CLEANUP_INTERVAL_MINUTES = 30 # Run the mega-cleanup every 30 minutes

# ==========================================
#      CPU-BOUND BACKGROUND PROCESSING
# ==========================================
def convert_wav_to_mp3(wav_path, mp3_path):
    try:
        audio = AudioSegment.from_wav(wav_path)
        audio.export(mp3_path, format="mp3")
        os.remove(wav_path)
        print(f" ✅ Converted: {os.path.basename(mp3_path)}")
    except Exception as e:
        print(f"\n❌ Audio Conversion Error on {wav_path}: {e}")

def download_file_sync(url, cookies, save_path):
    """Rock-solid synchronous download for massive files. Handles micro-stutters gracefully."""
    # Amazon S3 pre-signed URLs often reject requests if Salesforce cookies are attached
    use_cookies = cookies if "amazonaws.com" not in url.lower() else None
    
    # timeout=120 ensures it won't hang forever, but stream=True keeps it steady
    response = requests.get(url, cookies=use_cookies, stream=True, timeout=120)
    
    if response.status_code != 200:
        raise Exception(f"HTTP {response.status_code} - {response.reason}")
        
    with open(save_path, 'wb') as f:
        # 1MB chunks prevent memory overload and connection resets
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
    return True

# ==========================================
#               HELPER FUNCTIONS
# ==========================================
def get_session_from_auth_file():
    if not os.path.exists(AUTH_STATE_FILE):
        raise FileNotFoundError(f"❌ '{AUTH_STATE_FILE}' not found! Run 'a.py' first to authenticate.")

    with open(AUTH_STATE_FILE, "r") as f:
        auth_data = json.load(f)

    session_id = None
    instance_domain = "zebra.my.salesforce.com" 

    for cookie in auth_data.get("cookies", []):
        if cookie["name"] == "sid":
            if "my.salesforce.com" in cookie["domain"] or cookie["domain"] == "salesforce.com" or cookie["domain"] == ".salesforce.com":
                if "lightning" not in cookie["domain"]: 
                    session_id = cookie["value"]
                    break

    if not session_id:
        for cookie in auth_data.get("cookies", []):
            if cookie["name"] == "sid":
                session_id = cookie["value"]
                break

    if not session_id:
        raise ValueError("❌ Failed to find active session ID ('sid') in auth state file.")

    return session_id, instance_domain

async def close_popup_async(page, wait_time=2000):
    try:
        close_btn = page.get_by_role("button", name="Cancel and close")
        await close_btn.click(timeout=wait_time, force=True)
        await page.keyboard.press("Escape")
    except Exception:
        pass

async def update_savepoint(case_id, status, state_dict, lock):
    async with lock:
        state_dict[case_id] = status
        async with aiofiles.open(SAVEPOINT_FILE, 'w') as f:
            await f.write(json.dumps(state_dict, indent=4))

# 🧹 THE MEGA-CLEANUP FUNCTION
async def close_all_salesforce_tabs(context):
    """Opens a temporary tab and violently closes all saved Workspace tabs."""
    print("\n🧹 Initializing Salesforce Workspace Tab Cleanup...")
    page = await context.new_page()
    try:
        await page.goto(f"https://{SF_DOMAIN}/lightning/o/Case/list", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000) # Give Salesforce time to load all saved tabs
        await close_popup_async(page, 3000)
        
        closed_count = 0
        for _ in range(100): # Hard limit to prevent infinite loop
            try:
                # Use the exact selector you provided
                close_btn = page.get_by_title("Close Tab").first
                if await close_btn.is_visible(timeout=2000):
                    await close_btn.click(timeout=2000)
                    await page.wait_for_timeout(800) # Quick pause for animation
                    closed_count += 1
                else:
                    break # No more tabs!
            except Exception:
                break 
        
        print(f"✨ Cleanup complete! Destroyed {closed_count} saved tabs.\n")
    except Exception as e:
        print(f"⚠️ Cleanup skipped due to issue: {e}")
    finally:
        await page.close()

# ==========================================
#         ASYNC CASE PROCESSOR
# ==========================================
async def process_case(case_id, sf, context, semaphore, process_pool, pbar, state_dict, lock):
    async with semaphore:
        page = await context.new_page()
        case_status = "SUCCESS" 
        
        try:
            # --- STEP 1: API gets the Case ID ---
            case_query = f"SELECT Id FROM Case WHERE CaseNumber = '{case_id}'"
            case_result = await asyncio.to_thread(sf.query, case_query)
            
            if not case_result.get('records'):
                padded_case = str(case_id).zfill(8)
                case_query = f"SELECT Id FROM Case WHERE CaseNumber = '{padded_case}'"
                case_result = await asyncio.to_thread(sf.query, case_query)
                if not case_result.get('records'):
                    tqdm.write(f" ⚠️ Could not find internal ID for Case {case_id}")
                    await update_savepoint(case_id, "INVALID_CASE_ID", state_dict, lock)
                    return
                    
            real_case_id = case_result['records'][0]['Id']

            # --- STEP 2: API gets ALL Voice Calls ---
            vc_query = f"""
                SELECT Id, CallStartDateTime, CallType 
                FROM VoiceCall 
                WHERE RelatedRecordId = '{real_case_id}' 
                   OR Case__c = '{real_case_id}'
                   OR NF_Case_Number__c = '{real_case_id}'
            """
            try:
                vc_result = await asyncio.to_thread(sf.query, vc_query)
            except Exception:
                vc_fallback = f"""
                    SELECT Id, CallStartDateTime, CallType 
                    FROM VoiceCall 
                    WHERE RelatedRecordId = '{real_case_id}'
                       OR NF_Case_Number__c = '{real_case_id}'
                """
                vc_result = await asyncio.to_thread(sf.query, vc_fallback)
                
            vc_records = vc_result.get('records', [])

            # --- STEP 2.5: THE TASK FALLBACK ---
            if not vc_records:
                task_query = f"SELECT Id, Description, CallObject FROM Task WHERE WhatId = '{real_case_id}' AND TaskSubtype = 'Call'"
                try:
                    task_result = await asyncio.to_thread(sf.query, task_query)
                    task_records = task_result.get('records', [])
                    
                    vc_ids = []
                    for t in task_records:
                        if t.get('CallObject') and str(t['CallObject']).startswith('0LQ'):
                            vc_ids.append(t['CallObject'])
                        elif t.get('Description'):
                            match = re.search(r'0LQ[a-zA-Z0-9]{15}', t['Description'])
                            if match:
                                vc_ids.append(match.group(0))

                    if vc_ids:
                        formatted_ids = "', '".join(vc_ids)
                        vc_task_query = f"SELECT Id, CallStartDateTime, CallType FROM VoiceCall WHERE Id IN ('{formatted_ids}')"
                        vc_result = await asyncio.to_thread(sf.query, vc_task_query)
                        vc_records = vc_result.get('records', [])
                except Exception:
                    pass

            if not vc_records:
                tqdm.write(f" ⚠️ No Voice Calls found at all for Case {case_id}")
                await update_savepoint(case_id, "NO_VOICE_CALLS", state_dict, lock)
                return

            # --- STEP 3: Warp Directly to each Voice Call ---
            for index, vc in enumerate(vc_records):
                vc_id = vc['Id']
                vc_name = vc_id 
                
                call_type = vc['CallType'] if vc.get('CallType') else "UnknownType"
                raw_date = vc['CallStartDateTime']
                call_date = raw_date.split('.')[0].replace('T', ' ').replace(':', '.') if raw_date else "UnknownDate"

                wav_filename = os.path.join(DOWNLOAD_DIR, f"{case_id}_{vc_name}_{call_date}_{call_type}.wav")
                mp3_filename = os.path.join(DOWNLOAD_DIR, f"{case_id}_{vc_name}_{call_date}_{call_type}.mp3")

                if os.path.exists(mp3_filename):
                    continue

                # 🚀 DIRECT URL WARP
                direct_url = f"https://{SF_DOMAIN}/lightning/r/VoiceCall/{vc_id}/view"
                await page.goto(direct_url, wait_until="domcontentloaded")
                await close_popup_async(page)

                play_btn = page.get_by_role("button", name="Play")
                
                # --- FLEXIBLE NETWORK INTERCEPTOR ---
                intercepted_data = {"url": None}
                
                def handle_response(response):
                    try:
                        if ".wav" in response.url.lower() or "audio/wav" in response.headers.get("content-type", "").lower():
                            intercepted_data["url"] = response.url
                    except Exception:
                        pass

                page.on("response", handle_response)

                try:
                    await play_btn.wait_for(state="visible", timeout=10000)
                    await play_btn.click()
                except Exception:
                    pass 
                    
                for _ in range(20):
                    if intercepted_data["url"]: break
                    await asyncio.sleep(0.5)

                if not intercepted_data["url"]:
                    tqdm.write(f" ⚠️ Retrying Play button for {vc_name} (Popup block check)...")
                    await close_popup_async(page)
                    try:
                        await play_btn.click(force=True)
                    except Exception:
                        pass
                    
                    for _ in range(20):
                        if intercepted_data["url"]: break
                        await asyncio.sleep(0.5)
                        
                audio_url = intercepted_data["url"]

                if not audio_url:
                    tqdm.write(f" ❌ Failed to capture audio URL for {vc_name}.")
                    case_status = "UNPLAYABLE_CALLS"
                
                # --- SYNCHRONOUS CHUNKED DOWNLOAD (FIXES NETWORK CRASH) ---
                if audio_url:
                    cookies_list = await context.cookies()
                    cookies = {c['name']: c['value'] for c in cookies_list}
                    
                    try:
                        # Offload the stable requests.get download to a background thread
                        await asyncio.to_thread(download_file_sync, audio_url, cookies, wav_filename)
                        
                        # AWAIT the background CPU pool so tabs don't close before MP3 conversion finishes!
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(process_pool, convert_wav_to_mp3, wav_filename, mp3_filename)
                        
                    except Exception as download_err:
                        tqdm.write(f" ❌ Download stream error for {vc_name}: {repr(download_err)}")
                        case_status = "DOWNLOAD_ERROR"
                
                # --- 🧹 INDIVIDUAL TAB CLEANUP (Runs mid-stream to keep RAM low) ---
                try:
                    await page.get_by_role("button", name=re.compile(r"Close VC-", re.IGNORECASE)).first.click(timeout=2000)
                except Exception:
                    pass
                
                try:
                    await page.get_by_title("Close Tab").first.click(timeout=2000)
                except Exception:
                    pass
                
                page.remove_listener("response", handle_response)

            # 💾 Savepoint
            if case_status != "DOWNLOAD_ERROR":
                await update_savepoint(case_id, case_status, state_dict, lock)

        except Exception as e:
            tqdm.write(f" ❌ Network/Timeout Error processing Case {case_id}: {str(e)}")
            
        finally:
            await page.close()
            pbar.update(1)

# ==========================================
#              MAIN ORCHESTRATOR
# ==========================================
async def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    savepoint_state = {}
    if os.path.exists(SAVEPOINT_FILE):
        try:
            with open(SAVEPOINT_FILE, "r") as f:
                savepoint_state = json.load(f)
        except Exception:
            pass

    completed_cases_folder = set()
    for filename in os.listdir(DOWNLOAD_DIR):
        if filename.endswith(".mp3") and ("_Inbound" in filename or "_Outbound" in filename):
            parts = filename.split("_")
            if parts:
                completed_cases_folder.add(parts[0])

    try:
        df = pd.read_excel(EXCEL_FILE_PATH)
        all_cases = df[CASE_ID_COLUMN_NAME].dropna().astype(str).tolist()
        CASE_IDS = [c.replace('.0', '') for c in all_cases] 
    except Exception as e:
        print(f"❌ Error loading Excel: {e}")
        return

    active_case_ids = [c for c in CASE_IDS if c not in completed_cases_folder and c not in savepoint_state]
    
    print(f"📂 Pre-scan detected {len(completed_cases_folder)} completed cases in folder.")
    print(f"💾 Ledger detected {len(savepoint_state)} un-downloadable/finished cases.")
    print(f"🚀 Active Queue: {len(active_case_ids)} cases remaining.")

    if not active_case_ids:
        print("🎉 All cases have already been processed!")
        return

    print("\nExtracting session credentials from Playwright cookies...")
    try:
        session_id, instance_url = get_session_from_auth_file()
        sf = Salesforce(instance=instance_url, session_id=session_id)
        print("✅ REST API Connected Successfully!")
    except Exception as e:
        print(f"❌ REST API Connection Failed: {e}")
        return

    process_pool = ProcessPoolExecutor(max_workers=4)
    state_lock = asyncio.Lock()

    async with async_playwright() as p:
        # 🎯 Change headless=True to completely hide Google Chrome in the background
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=AUTH_STATE_FILE)
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_TABS) 

        # 🧹 INITIAL MEGA-CLEANUP: Closes any leftover tabs from previous aborted runs
        await close_all_salesforce_tabs(context)

        print(f"\n⚡ Firing up Async Engine with {MAX_CONCURRENT_TABS} concurrent tabs...")
        
        # ⏱️ BATCH PROCESSING SYSTEM (Handles the 30-minute intermission)
        BATCH_SIZE = 50 # Process 50 cases, then check the clock
        last_cleanup_time = time.time()
        
        with tqdm(total=len(active_case_ids), desc="Processing Cases", unit="case") as pbar:
            for i in range(0, len(active_case_ids), BATCH_SIZE):
                batch_cases = active_case_ids[i:i+BATCH_SIZE]
                
                tasks = [
                    process_case(case_id, sf, context, semaphore, process_pool, pbar, savepoint_state, state_lock) 
                    for case_id in batch_cases
                ]
                # Wait for the current batch to fully finish
                await asyncio.gather(*tasks)
                
                # ⏱️ 30-MINUTE CHECK
                if time.time() - last_cleanup_time > (CLEANUP_INTERVAL_MINUTES * 60):
                    tqdm.write(f"\n⏳ 30 Minutes elapsed! Pausing downloads to clean up Salesforce tabs...")
                    await close_all_salesforce_tabs(context)
                    last_cleanup_time = time.time()
                    tqdm.write("▶️ Resuming downloads...\n")

        await browser.close()
    
    process_pool.shutdown()
    print("\n🎉 All Cases Processed successfully using the Async God-Tier Method!")

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
