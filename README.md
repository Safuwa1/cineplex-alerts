# Cineplex Alert System — Phase 1 Setup

No coding required. Just follow these steps in order. Total time: about 10 minutes.

## Step 1 — Create a free GitHub account (skip if you already have one)
Go to https://github.com/signup and create a free account.

## Step 2 — Create a new repository
1. Go to https://github.com/new
2. Repository name: `cineplex-alerts`
3. Keep it **Private**
4. Click **Create repository**

## Step 3 — Upload these files
On the new repository's page, click **"uploading an existing file"** (or **Add file → Upload files**).

Drag the entire unzipped `cineplex-alerts` folder — including the hidden `.github` folder — into the upload box. Your browser will preserve the folder structure automatically.

Click **Commit changes**.

Your repository should end up looking like this:
```
cineplex-alerts/
├── discover.py
├── requirements.txt
└── .github/
    └── workflows/
        └── discover.yml
```

## Step 4 — Create a Gmail "App Password" (this is what lets the robot send email as you)
1. Go to https://myaccount.google.com/security and turn on **2-Step Verification** if it isn't already on.
2. Go to https://myaccount.google.com/apppasswords
3. Create a new app password (name it anything, e.g. "Cineplex Alerts")
4. Copy the 16-character password shown — you will not be able to see it again.

*(Don't use Gmail? Tell me your email provider in our chat and I'll adjust this step.)*

## Step 5 — Add 3 secrets to your repository
In your repository: **Settings → Secrets and variables → Actions → New repository secret**

Add these three, one at a time:

| Secret name | Value |
|---|---|
| `SENDER_EMAIL` | Your Gmail address |
| `SENDER_APP_PASSWORD` | The 16-character app password from Step 4 |
| `RECEIVER_EMAIL` | The email address you want alerts sent **to** (can be the same Gmail, or any other address) |

## Step 6 — Run it
1. Go to the **Actions** tab of your repository.
2. If prompted, click **"I understand my workflows, go ahead and enable them"**.
3. Click **"Cineplex Discovery (Phase 1)"** in the left sidebar.
4. Click the **Run workflow** button (top right) → **Run workflow**.
5. Wait about 1–2 minutes. Refresh the page — a green checkmark means it worked.

## Step 7 — Check your email
Look for an email titled **"[Cineplex Alert Setup] Discovery results"**.

## Step 8 — Come back to our chat
Copy the content of that email and paste it back to me here. I'll use it to build the final version — the one that actually watches for new movies and new ticket dates, and emails you automatically, on a schedule, with no further action needed from you.

---
**Troubleshooting:**
- Red X instead of green check on the Actions run? Click into the run, open the failing step, and paste me the error text.
- No email arrived? Double check the 3 secret values for typos (secret values can't be viewed again once saved — if unsure, delete and re-add them).
