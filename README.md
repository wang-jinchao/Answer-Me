# Answer-Me Automation

This repository runs browser automation to perform the daily tasks on the target site and saves a daily report.

Quick start

1. Provide required environment variables (do not store real secrets in the repo):
   - ACCOUNTS_JSON: a JSON string containing account objects. Example:
     ACCOUNTS_JSON='[{"name":"user001","username":"RS2789","password":"REPLACE_ME"}]'
   - TARGET_URL: the URL the automation should navigate to.
   - Optional: MAX_WORKERS to control parallelism (default: 4).

2. Install dependencies and Playwright browsers:

   pip install -r requirements.txt
   playwright install chromium

3. Run tests:

   python -m pytest tests

4. Run locally:

   python src/main.py

GitHub Actions

The .github/workflows/daily.yml demonstrates running this in CI using repository secrets for ACCOUNTS_JSON and TARGET_URL. Ensure you add those secrets in repository settings.

Notes and safety

- Do NOT commit real credentials. Use GitHub Secrets for CI or .env files kept out of source control when running locally.
- This project now implements login + captcha handling and the daily task flows for 每日一学, 每日一看, 每日一练, and 每日一答.
- Logging is used instead of prints; reports are saved under the `reports/` directory.
