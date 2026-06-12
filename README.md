# Optibus Payroll Endpoint Tester

## What this project does

This project provides a GitHub-ready Streamlit app for testing the Optibus payroll endpoint across a selected period.

The app is designed for troubleshooting. It runs payroll calls in date and driver chunks, records any request failures, keeps going, and exports one consolidated errors CSV at the end so issues can be fixed in bulk instead of one by one.

## Proposed architecture

The app is split into the following layers:

- `streamlit_app.py`: Streamlit UI
- `optibus_payroll_compare/api.py`: API client and data-fetching logic
- `optibus_payroll_compare/processing.py`: CSV shaping and ZIP creation
- `optibus_payroll_compare/pipeline.py`: endpoint-test orchestration
- `optibus_payroll_compare/models.py` and `utils.py`: shared data structures and helpers

This removes local-only UI assumptions such as AppleScript prompts, `input()`, and macOS Keychain storage from the core logic.

## Repo structure

```text
.
├── .env.example
├── .gitignore
├── README.md
├── requirements.txt
├── streamlit_app.py
└── optibus_payroll_compare
    ├── __init__.py
    ├── api.py
    ├── models.py
    ├── pipeline.py
    ├── processing.py
    └── utils.py
```

## Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Environment variables

You can set these locally in a `.env` file or in your shell:

```bash
OPTIBUS_BASE_URL=https://YOUR-ACCOUNT.api.ops.optibus.co
OPTIBUS_API_CLIENT=YOUR_ACCOUNT_NAME
OPTIBUS_API_KEY=YOUR_API_KEY
```

A sample template is included in `.env.example`.

## How to run locally

```bash
streamlit run streamlit_app.py
```

Then:

1. Enter your connection details if they are not already in environment variables
2. Choose the start and end date
3. Optionally provide paycodes, batch overrides, parallel request overrides, or cache usage
4. Click **Run payroll endpoint test**
5. Download the CSVs or ZIP bundle

## Example usage

Typical local usage:

```bash
export OPTIBUS_BASE_URL="https://YOUR-ACCOUNT.api.ops.optibus.co"
export OPTIBUS_API_CLIENT="ADO"
export OPTIBUS_API_KEY="YOUR_API_KEY"
streamlit run streamlit_app.py
```

## Streamlit deployment notes

### Streamlit Community Cloud

Set the following in the app settings or secrets:

- `OPTIBUS_BASE_URL`
- `OPTIBUS_API_CLIENT`
- `OPTIBUS_API_KEY`

This app writes outputs to a temporary directory for the current session.

### Paths and secrets

- No hardcoded local paths are used
- No macOS-only AppleScript or Keychain features remain
- Credentials are read from Streamlit inputs, environment variables, or Streamlit secrets/environment settings

## Outputs

- Payroll CSV containing successful responses returned by the endpoint
- Errors CSV containing all captured request failures, including request range, chunk context, status code, and response excerpt
- ZIP bundle containing both output files

## Notes

- The app runs across **all regions/depots in the account** by fetching all drivers for the selected date
- Driver and date chunking are preserved to reduce 413 errors
- Multiple payroll requests can run in parallel to speed up larger runs, and the parallelism can be fine-tuned in the app config
- The app continues after individual payroll request failures instead of stopping the full run
