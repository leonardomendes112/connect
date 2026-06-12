from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from optibus_payroll_compare.models import RunParameters
from optibus_payroll_compare.pipeline import run_payroll_endpoint_test
from optibus_payroll_compare.utils import mask_api_key

load_dotenv()

st.set_page_config(page_title="Optibus Payroll Endpoint Tester", layout="wide")
st.title("Optibus Payroll Endpoint Tester")
st.caption(
    "Run the payroll endpoint for a full period, continue after individual request failures, and export one "
    "compiled error file at the end."
)

if "test_result" not in st.session_state:
    st.session_state.test_result = None
if "output_dir" not in st.session_state:
    st.session_state.output_dir = tempfile.mkdtemp(prefix="optibus_payroll_endpoint_test_")


def build_params() -> RunParameters:
    """Read user inputs into a RunParameters object."""
    return RunParameters(
        base_url=st.session_state.base_url,
        api_key=st.session_state.api_key,
        api_client=st.session_state.api_client,
        start_date=st.session_state.start_date.isoformat() if st.session_state.start_date else "",
        end_date=st.session_state.end_date.isoformat() if st.session_state.end_date else "",
        batch_days=int(st.session_state.batch_days) if st.session_state.batch_days else None,
        driver_chunk_size=int(st.session_state.driver_chunk_size) if st.session_state.driver_chunk_size else None,
        max_parallel_requests=(
            int(st.session_state.max_parallel_requests) if st.session_state.max_parallel_requests else None
        ),
        paycodes_csv=st.session_state.paycodes_csv.strip(),
        should_use_cache=st.session_state.should_use_cache,
    )


def make_logger(container):
    """Create a logger that appends messages to a Streamlit code block."""
    messages: list[str] = []

    def log(message: str) -> None:
        messages.append(message)
        container.code("\n".join(messages))

    return log


with st.sidebar:
    st.subheader("Connection")
    st.text_input(
        "Base URL",
        key="base_url",
        value=os.getenv("OPTIBUS_BASE_URL", ""),
        help="Example: https://YOUR-ACCOUNT.api.ops.optibus.co",
    )
    st.text_input(
        "API Client",
        key="api_client",
        value=os.getenv("OPTIBUS_API_CLIENT", ""),
        help='Value for the X-Optibus-Api-Client header.',
    )
    st.text_input(
        "API Key",
        key="api_key",
        value=os.getenv("OPTIBUS_API_KEY", ""),
        type="password",
        help="Stored only in this session unless you use Streamlit secrets or environment variables.",
    )

    st.subheader("Run options")
    st.date_input("Start date", key="start_date", value=None, format="YYYY-MM-DD")
    st.date_input("End date", key="end_date", value=None, format="YYYY-MM-DD")
    st.text_input(
        "Paycodes (optional)",
        key="paycodes_csv",
        value="",
        help="Comma-separated paycodes. Leave blank to fetch all paycodes.",
    )
    st.number_input(
        "Batch days (optional override)",
        key="batch_days",
        min_value=0,
        value=0,
        step=1,
        help="Use 0 to let the app auto-tune this value.",
    )
    st.number_input(
        "Driver chunk size (optional override)",
        key="driver_chunk_size",
        min_value=0,
        value=0,
        step=1,
        help="Use 0 to let the app auto-tune this value.",
    )
    st.number_input(
        "Parallel requests (optional override)",
        key="max_parallel_requests",
        min_value=0,
        max_value=32,
        value=0,
        step=1,
        help="Use 0 to auto-tune how many payroll calls run at the same time.",
    )
    st.checkbox(
        "Use cached payroll results",
        key="should_use_cache",
        value=False,
        help="Use cached payroll results when the endpoint supports it for this test run.",
    )

    if st.button("Clear session state", use_container_width=True):
        st.session_state.test_result = None
        st.rerun()

if st.session_state.api_key:
    st.info(f"Using API key: {mask_api_key(st.session_state.api_key)}")

st.subheader("Run test")
st.write(
    "The app will call the payroll endpoint across the selected period, keep moving after failed requests, and "
    "produce a single errors CSV with all captured issues."
)
if st.button("Run payroll endpoint test", type="primary", use_container_width=True):
    params = build_params()
    log_box = st.empty()
    logger = make_logger(log_box)
    try:
        output_dir = Path(st.session_state.output_dir)
        test_result = run_payroll_endpoint_test(params=params, output_dir=output_dir, log=logger)
        st.session_state.test_result = test_result
        st.success("Payroll endpoint test complete.")
    except Exception as exc:
        st.exception(exc)

if st.session_state.test_result is not None:
    test_result = st.session_state.test_result
    st.divider()
    st.subheader("Outputs")
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Regions", test_result.region_count)
    col2.metric("Drivers", test_result.driver_count)
    col3.metric("Successful payroll calls", test_result.success_call_count)
    col4.metric("Payroll rows", test_result.payroll_rows)
    col5.metric("Errors captured", test_result.error_count)
    st.caption(
        f"Run settings used: batch_days={test_result.batch_days}, "
        f"driver_chunk_size={test_result.driver_chunk_size}, "
        f"parallel_requests={test_result.max_parallel_requests}"
    )

    for file_path in [test_result.payroll_path, test_result.errors_path]:
        with open(file_path, "rb") as handle:
            st.download_button(
                label=f"Download {file_path.name}",
                data=handle.read(),
                file_name=file_path.name,
                mime="text/csv",
            )

    with open(test_result.zip_path, "rb") as handle:
        st.download_button(
            label="Download payroll test outputs (.zip)",
            data=handle.read(),
            file_name=test_result.zip_path.name,
            mime="application/zip",
            type="primary",
        )

    st.subheader("Errors preview")
    errors_df = pd.read_csv(test_result.errors_path, encoding="utf-8-sig").head(100)
    if errors_df.empty:
        st.success("No payroll endpoint errors were captured for this run.")
    else:
        st.dataframe(errors_df, use_container_width=True)

st.divider()
st.markdown(
    """
**Notes**

- This app intentionally follows the current script behavior and runs across all regions/depots in the account.
- The original Mac-only AppleScript dialogs and Keychain persistence were removed because they are not suitable for Streamlit deployment.
- For Streamlit Community Cloud, put credentials in the app secrets or environment variables rather than typing them every time.
"""
)
