import asyncio
import os
import pandas as pd
from tqdm import tqdm
from geminion import GeminiNetworkDriver
from geminion.config import GeminionConfig

async def process_transcripts():
    excel_file = "Latest Creation by split for Ignore ZSU.xlsx"
    downloads_folder = "downloads"
    
    # 1. Load the Excel file via Pandas
    print("Loading Excel data...")
    df = pd.read_excel(excel_file)
    
    # Create the 'Transcript' column if it doesn't exist
    if "Transcript" not in df.columns:
        df["Transcript"] = None
        
    # Create a temporary clean case ID column for perfect matching
    df['clean_case'] = df['CASE_ID'].astype(str).str.strip().str.replace('.0', '', regex=False)

    # 2. Scan the downloads folder and limit to exactly 100 MP3s
    all_files = os.listdir(downloads_folder)
    mp3_files = [f for f in all_files if f.endswith('.mp3')][:100]
    
    if not mp3_files:
        print("No MP3 files found in the downloads folder.")
        return

    print(f"Found {len(mp3_files)} MP3 files. Starting Gemini transcription pipeline (1 Worker)...")

    # 3. Initialize the Gemini Enterprise Driver (Sequential)
    config = (
        GeminionConfig()
        .with_model_name("3.1 Pro")
        .with_concurrent_workers(1)
    )
    driver = GeminiNetworkDriver(config=config)
    
    # 4. Iterate STRICTLY sequentially over the 100 MP3 files
    for i, mp3_file in enumerate(tqdm(mp3_files, desc="Transcribing & Translating Audio")):
        case_id = mp3_file.replace('.mp3', '')
        mp3_path = os.path.join(downloads_folder, mp3_file)
        
        # Find matching row in Excel
        matching_rows = df.index[df['clean_case'] == case_id].tolist()
        if not matching_rows:
            tqdm.write(f"⚠️ Case ID {case_id} from audio not found in Excel. Skipping.")
            continue
            
        index = matching_rows[0] # Target the exact Excel row
        
        # Skip if we already have a transcript for this row
        if pd.notna(df.at[index, "Transcript"]) and str(df.at[index, "Transcript"]).strip() != "":
            continue

        # 5. Send MP3 to Gemini with the SPEAKER-DIARIZED Translation Prompt
        try:
            result = await driver.prompt(
                prompt_text=(
                    "Please listen to this entire call recording and provide a highly accurate, English-translated transcript. "
                    "\n\n"
                    "CRITICAL INSTRUCTIONS:\n"
                    "1. SPEAKER IDENTIFICATION: Based on the context of the conversation and the voices, identify whether "
                    "the person talking is the 'Agent' (the support representative) or the 'Customer' (the caller). "
                    "Clearly label each turn of the conversation, using 'Agent:' or 'Customer:' at the beginning of each line.\n"
                    "2. TRANSLATION: If any part of the audio contains languages other than English, you MUST translate it. "
                    "The final output must be 100% IN ENGLISH.\n"
                    "3. VERBATIM & COMPLETENESS: Do NOT stop in the middle of the audio. You must transcribe and translate every "
                    "spoken word from the very beginning to the absolute end of the recording.\n"
                    "4. FORMATTING: Output ONLY the labeled English transcript text. Do not include any conversational filler "
                    "like 'Here is the transcript' or introduction text."
                ),
                force_text=True,
                attachments=[mp3_path]
            )
            
            if result["status"] == "success":
                transcript_text = result["payload"]["text"]
                df.at[index, "Transcript"] = transcript_text
            else:
                tqdm.write(f"❌ Failed to process Case {case_id}: {result.get('error')}")
                
            # Pause slightly between requests to avoid rate limits
            await asyncio.sleep(2)
                
        except Exception as e:
            tqdm.write(f"🛑 Error processing Case {case_id}: {str(e)}")
            
        # Periodic Checkpoint: Save every 5 files to prevent data loss
        if i > 0 and i % 5 == 0:
            df.drop(columns=['clean_case']).to_excel(excel_file, index=False)

    # Final Save and Cleanup (dropping the temporary matching column)
    df.drop(columns=['clean_case']).to_excel(excel_file, index=False)
    await driver.close()
    print(f"\n✅ Pipeline Complete! Labeled English Transcripts safely written to {excel_file}")

if __name__ == "__main__":
    asyncio.run(process_transcripts())
