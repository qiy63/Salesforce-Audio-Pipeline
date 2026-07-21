# Salesforce Call Audio Downloader & Transcriber

This repository contains an automated data processing pipeline designed to interact with Salesforce. It authenticates, downloads call recordings associated with specific cases, verifies the downloads, and transcribes the audio using Gemini AI.

## 🎯 The End Goal

To create a fully automated, zero-touch pipeline that transforms locked away, multi-lingual Salesforce audio recordings into a structured, easily searchable, and fully audited English text database. This enables rapid analysis of customer interactions without requiring manual downloading, listening, or manual translation.

## 💡 What it is Used For

* **Quality Assurance & Compliance:** Automatically pulling random samples of calls for auditing and ensuring agents are following proper procedures.
* **Sentiment & Issue Analysis:** Converting audio to text so teams can run keyword searches or sentiment analysis on customer complaints.
* **Agent Training:** Building a repository of verbatim transcripts (translated to English) to use as examples in training sessions.
* **Process Automation:** Eliminating the tedious, manual process of clicking through Salesforce to find, play, and save individual audio files.

---

## 📊 How It Works (System Architecture)

```mermaid
graph TD
    subgraph Initialization
        A[Workbench SOQL Query] -->|Extracts Case/Call IDs| B(Input Excel File)
        C[auth.py] -->|Playwright MFA Login| D{sf_auth.json Session Key}
    end

    subgraph Data Extraction
        B --> E[api.py]
        D --> E
        E -->|Scrapes Salesforce UI/API| F[Local Downloads Folder .mp3]
    end

    subgraph Audit & Verification
        D --> G[checker.py]
        F --> G
        B --> G
        G -->|Cross-references files vs SF| H[Audit Report Excel]
    end

    subgraph AI Transcription
        F --> I[transcribe_cases.py]
        I -->|Sends Audio| J((Gemini AI API))
        J -->|Returns English Translation & Speakers| I
        I -->|Appends Transcripts to| B
    end

## 🛠 Setup & Prerequisites
* Python 3.x
* Playwright (pip install playwright followed by playwright install)
* Required Python packages (e.g., pandas, requests, Gemini SDK)
* A valid Gemini API Key

## 🚀 Usage
* Important Note on Data Extraction: The initial Call IDs and Case IDs used as inputs for this pipeline must be extracted using Workbench SOQL queries directly from Salesforce and saved into your input Excel file.
* Run auth.py to authenticate and create your session token. (Wait for MFA prompt on your device).
* Ensure your input Excel file with Case/Call IDs is in the root directory.
* Run api.py to fetch and download the audio.
* Run checker.py to verify all files were downloaded successfully.
* Run transcribe_cases.py to process the audio and append transcripts to your Excel file.