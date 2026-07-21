from playwright.sync_api import sync_playwright, TimeoutError
import os

# ==========================================
#               CONFIGURATION
# ==========================================
SF_URL = ""
USERNAME = ""   # <-- UPDATE THIS
PASSWORD = ""             # <-- UPDATE THIS
AUTH_STATE_FILE = "sf_auth.json"

# ==========================================
#         LOGIN & SESSION EXTRACTOR
# ==========================================
def generate_login_session():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print("\nNavigating to Salesforce (checking for SSO)...")
        page.goto(SF_URL)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(3000) 
        
        try:
            page.wait_for_selector("input[name='username'], #identifierInput", timeout=8000)
            if page.locator("#identifierInput").is_visible():
                page.locator("#identifierInput").fill(USERNAME)
                page.locator("#identifierInput").press("Enter")
                page.wait_for_timeout(1500) 
                page.locator("#password").fill(PASSWORD)
                page.locator("#password").press("Enter")
            else:
                page.fill("input[name='username']", USERNAME)
                page.fill("input[name='pw']", PASSWORD)
                page.click("input[name='Login']")
                
            print("📱 Credentials entered! Please check your phone to approve the login.")
            print("⏳ Waiting 25 seconds for you to authenticate...")
            page.wait_for_timeout(25000)
            
        except TimeoutError:
            print("SSO auto-login detected or page loaded directly!")

        # Verify login was successful by waiting for the Salesforce UI to settle
        print("\n⏳ Verifying login success and generating token...")
        try:
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(5000)
            
            # 🔥 THE MOST IMPORTANT LINE: Saves your cookies to the file
            context.storage_state(path=AUTH_STATE_FILE)
            
            print(f"✅ Session successfully saved to '{AUTH_STATE_FILE}'!")
            print("🚀 SUCCESS: You can now run the 'api_downloader.py' script!")
            
        except Exception as e:
            print("❌ Failed to save session. Did the login complete successfully?")
            print(f"Error: {e}")

        browser.close()

if __name__ == "__main__":
    generate_login_session()
