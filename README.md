# Answer-Me Automation

This repository runs simple browser automation over configured accounts and saves a daily report.

Quick start

1. Provide required environment variables (do not store real secrets in the repo):
   - ACCOUNTS_JSON: a JSON string containing account objects. Example:
     ACCOUNTS_JSON='[{"name":"user001","username":"RS2789","password":"REPLACE_ME"}]'
   - TARGET_URL: the URL the automation should navigate to.
   - Optional: MAX_WORKERS to control parallelism (default: 4).

2. Install dependencies and Playwright browsers:

   pip install -r requirements.txt
   playwright install chromium

3. Run locally:

   python src/main.py

GitHub Actions

The .github/workflows/daily.yml demonstrates running this in CI using repository secrets for ACCOUNTS_JSON and TARGET_URL. Ensure you add those secrets in repository settings.

Notes and safety

- Do NOT commit real credentials. Use GitHub Secrets for CI or .env files kept out of source control when running locally.
- The project includes a generic automation skeleton. You must implement the domain-specific steps in src/account_runner.py to match your application's selectors and flows.
- Logging is used instead of prints; reports are saved under the `reports/` directory.

If you want, this repository can be extended with unit tests and CI checks — open an issue or request and they will be added.