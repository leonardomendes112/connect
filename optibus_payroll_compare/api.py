from __future__ import annotations

import concurrent.futures
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Optional

import requests

from .models import DriverInfo, PayrollTestError
from .utils import date_batches, iso_date, safe_str


class OptibusError(RuntimeError):
    """Raised when the Optibus API returns an unexpected error."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        url: str = "",
        params: Optional[dict[str, Any]] = None,
        response_text: str = "",
        response_json: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.params = dict(params or {})
        self.response_text = response_text
        self.response_json = response_json


class OptibusClient:
    """Small HTTP client for the Optibus external API."""

    def __init__(self, base_url: str, api_key: str, api_client: str, timeout_s: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_client = api_client
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": api_key,
                "X-Optibus-Api-Client": api_client,
            }
        )

    def clone(self) -> "OptibusClient":
        """Create a fresh client instance for parallel requests."""
        return OptibusClient(
            base_url=self.base_url,
            api_key=self.api_key,
            api_client=self.api_client,
            timeout_s=self.timeout_s,
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def get_json(self, path: str, params: Optional[dict[str, Any]] = None, allow_413: bool = False) -> Any:
        """Run a GET request and parse JSON when available."""
        url = self._url(path)
        response = self.session.get(url, params=params, timeout=self.timeout_s, allow_redirects=True)
        if response.status_code in (413, 414) and allow_413:
            return {
                "__HTTP_413__": True,
                "__status__": response.status_code,
                "__text__": response.text,
                "__json__": self._maybe_json(response),
            }
        if response.status_code >= 400:
            raise OptibusError(
                f"HTTP {response.status_code} for GET {url} params={params} body={response.text[:800]}",
                status_code=response.status_code,
                url=url,
                params=params,
                response_text=response.text[:4000],
                response_json=self._maybe_json(response),
            )
        return self._maybe_json(response)

    @staticmethod
    def _maybe_json(response: requests.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return response.text


def fetch_regions(client: OptibusClient) -> list[dict]:
    """Fetch regions or wrapped region payloads."""
    data = client.get_json("/v1/regions")
    if isinstance(data, dict):
        for key in ("regions", "data", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return []
    return data if isinstance(data, list) else []


def fetch_all_drivers(client: OptibusClient, on_date: str) -> list[dict]:
    """Fetch all drivers using the paginated /v2/drivers endpoint."""
    page = 1
    all_rows: list[dict] = []
    while True:
        payload = client.get_json("/v2/drivers", params={"page": page, "onDate": on_date})
        drivers = payload.get("drivers", []) if isinstance(payload, dict) else []
        all_rows.extend(drivers)
        pagination = payload.get("pagination", {}) if isinstance(payload, dict) else {}
        current_page = pagination.get("currentPage", page)
        total_pages = pagination.get("totalPages", current_page)
        if current_page >= total_pages:
            break
        page += 1
    return all_rows


def fetch_driver_day_labels(client: OptibusClient, start: date, end: date) -> list[dict]:
    """Fetch driver day labels for the selected date range."""
    payload = client.get_json(
        "/v1/calendar-driver-day-labels",
        params={"fromDate": iso_date(start), "toDate": iso_date(end)},
    )
    return payload if isinstance(payload, list) else []


def build_driver_maps(drivers_payload: list[dict]) -> tuple[dict[str, DriverInfo], dict[str, DriverInfo]]:
    """Return lookup dictionaries by UUID and by external driver ID."""
    by_uuid: dict[str, DriverInfo] = {}
    by_external_id: dict[str, DriverInfo] = {}

    for driver in drivers_payload:
        uuid = safe_str(driver.get("uuid") or driver.get("driverUuid") or driver.get("id"))
        external_id = safe_str(driver.get("id") or driver.get("externalId") or driver.get("driverExternalId"))
        first_name = safe_str(driver.get("firstName"))
        last_name = safe_str(driver.get("lastName"))
        main_region_period = driver.get("mainRegionPeriod") or {}
        depot_name = safe_str(main_region_period.get("depotName") or driver.get("depotName"))
        region_name = safe_str(main_region_period.get("regionName") or driver.get("regionName"))

        if not uuid or not external_id:
            continue

        info = DriverInfo(
            external_id=external_id,
            uuid=uuid,
            first_name=first_name,
            last_name=last_name,
            depot_name=depot_name,
            region_name=region_name,
        )
        by_uuid[uuid] = info
        by_external_id[external_id] = info

    return by_uuid, by_external_id


def _chunk_list(items: list[str], chunk_size: int) -> list[list[str]]:
    if chunk_size <= 0:
        return [items]
    return [items[index : index + chunk_size] for index in range(0, len(items), chunk_size)]


def _driver_ids_preview(driver_ids: list[str], limit: int = 20) -> str:
    if not driver_ids:
        return ""
    preview = ", ".join(str(driver_id) for driver_id in driver_ids[:limit])
    if len(driver_ids) > limit:
        preview = f"{preview} ... (+{len(driver_ids) - limit} more)"
    return preview


def _response_excerpt(payload: Any) -> str:
    text = safe_str(payload).strip()
    return text[:4000]


def _noop_log(_: str) -> None:
    """Avoid writing to the Streamlit logger from worker threads."""
    return None


def fetch_payroll_chunked(
    client: OptibusClient,
    start: date,
    end: date,
    driver_ids: list[str],
    batch_days: int,
    should_use_cache: bool,
    depot_id: Optional[str] = None,
    driver_chunk_size: int = 50,
    paycodes: Optional[list[str]] = None,
    sleep_seconds: float = 0.0,
    log=print,
) -> list[dict]:
    """Fetch payroll in date and driver chunks to reduce 413 errors."""
    all_rows: list[dict] = []
    batches = date_batches(start, end, batch_days)
    driver_chunks = _chunk_list(driver_ids, driver_chunk_size) if driver_ids else [[]]

    for batch_start, batch_end in batches:
        log(f"Payroll batch {iso_date(batch_start)} -> {iso_date(batch_end)}")
        for index, driver_chunk in enumerate(driver_chunks, start=1):
            if driver_ids:
                log(f"  Drivers chunk {index}/{len(driver_chunks)} (drivers={len(driver_chunk)})")
            rows = _fetch_payroll_range_resilient(
                client=client,
                start=batch_start,
                end=batch_end,
                driver_ids=driver_chunk,
                should_use_cache=should_use_cache,
                depot_id=depot_id,
                paycodes=paycodes,
                log=log,
            )
            all_rows.extend(rows)
            if sleep_seconds:
                import time

                time.sleep(sleep_seconds)

    return all_rows


def fetch_payroll_chunked_collect_errors(
    client: OptibusClient,
    start: date,
    end: date,
    driver_ids: list[str],
    batch_days: int,
    should_use_cache: bool,
    depot_id: Optional[str] = None,
    driver_chunk_size: int = 50,
    max_workers: int = 1,
    paycodes: Optional[list[str]] = None,
    sleep_seconds: float = 0.0,
    log=print,
) -> tuple[list[dict], list[PayrollTestError], int]:
    """Fetch payroll while collecting request errors instead of stopping the full run."""
    batches = date_batches(start, end, batch_days)
    driver_chunks = _chunk_list(driver_ids, driver_chunk_size) if driver_ids else [[]]
    work_items = [
        (sequence, batch_start, batch_end, chunk_index, driver_chunk)
        for sequence, (batch_start, batch_end, chunk_index, driver_chunk) in enumerate(
            (
                (batch_start, batch_end, chunk_index, driver_chunk)
                for batch_start, batch_end in batches
                for chunk_index, driver_chunk in enumerate(driver_chunks, start=1)
            )
        )
    ]

    if max_workers <= 1 or len(work_items) <= 1:
        all_rows: list[dict] = []
        all_errors: list[PayrollTestError] = []
        success_call_count = 0
        for _, batch_start, batch_end, index, driver_chunk in work_items:
            log(f"Payroll batch {iso_date(batch_start)} -> {iso_date(batch_end)}")
            if driver_ids:
                log(f"  Drivers chunk {index}/{len(driver_chunks)} (drivers={len(driver_chunk)})")
            rows, errors, chunk_successes = _fetch_payroll_range_collecting_errors(
                client=client,
                start=batch_start,
                end=batch_end,
                driver_ids=driver_chunk,
                should_use_cache=should_use_cache,
                depot_id=depot_id,
                paycodes=paycodes,
                driver_chunk_index=index,
                driver_chunk_count=len(driver_chunks),
                log=log,
            )
            all_rows.extend(rows)
            all_errors.extend(errors)
            success_call_count += chunk_successes
            if errors:
                log(f"  Recorded {len(errors)} error(s) for this chunk and continued.")
            if sleep_seconds:
                import time

                time.sleep(sleep_seconds)
        return all_rows, all_errors, success_call_count

    worker_count = min(max_workers, len(work_items))
    log(f"Using up to {worker_count} concurrent payroll request(s).")

    def run_item(
        sequence: int,
        batch_start: date,
        batch_end: date,
        chunk_index: int,
        driver_chunk: list[str],
    ) -> tuple[int, list[dict], list[PayrollTestError], int, str]:
        worker_client = client.clone()
        rows, errors, chunk_successes = _fetch_payroll_range_collecting_errors(
            client=worker_client,
            start=batch_start,
            end=batch_end,
            driver_ids=driver_chunk,
            should_use_cache=should_use_cache,
            depot_id=depot_id,
            paycodes=paycodes,
            driver_chunk_index=chunk_index,
            driver_chunk_count=len(driver_chunks),
            log=_noop_log,
        )
        summary = (
            f"Payroll batch {iso_date(batch_start)} -> {iso_date(batch_end)} | "
            f"chunk {chunk_index}/{len(driver_chunks)} (drivers={len(driver_chunk)}) | "
            f"successes={chunk_successes} | errors={len(errors)}"
        )
        return sequence, rows, errors, chunk_successes, summary

    completed: dict[int, tuple[list[dict], list[PayrollTestError], int]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(run_item, sequence, batch_start, batch_end, chunk_index, driver_chunk): sequence
            for sequence, batch_start, batch_end, chunk_index, driver_chunk in work_items
        }
        for future in concurrent.futures.as_completed(future_map):
            sequence, rows, errors, chunk_successes, summary = future.result()
            completed[sequence] = (rows, errors, chunk_successes)
            log(summary)
            if errors:
                log("  Recorded error(s) for this chunk and continued.")

    all_rows = []
    all_errors = []
    success_call_count = 0
    for sequence in sorted(completed):
        rows, errors, chunk_successes = completed[sequence]
        all_rows.extend(rows)
        all_errors.extend(errors)
        success_call_count += chunk_successes

    return all_rows, all_errors, success_call_count


def _fetch_payroll_range_resilient(
    client: OptibusClient,
    start: date,
    end: date,
    driver_ids: list[str],
    should_use_cache: bool,
    depot_id: Optional[str] = None,
    paycodes: Optional[list[str]] = None,
    log=print,
) -> list[dict]:
    """Fetch one date range and recursively split drivers/dates if the API rejects the request size."""
    params: dict[str, Any] = {
        "startDate": iso_date(start),
        "endDate": iso_date(end),
        "shouldUseCache": "true" if should_use_cache else "false",
    }

    if paycodes:
        params["paycodes"] = ",".join(str(code) for code in paycodes)

    if driver_ids:
        params["driverIds"] = ",".join(str(driver_id) for driver_id in driver_ids)
    else:
        if not depot_id:
            raise OptibusError("depot_id is required when driver_ids is empty for /v2/payroll.")
        params["depotId"] = depot_id

    payload = client.get_json("/v2/payroll", params=params, allow_413=True)

    if isinstance(payload, dict) and payload.get("__HTTP_413__"):
        if driver_ids and len(driver_ids) > 1:
            mid = len(driver_ids) // 2
            left = driver_ids[:mid]
            right = driver_ids[mid:]
            log(f"  413 too large. Splitting drivers: {len(driver_ids)} -> {len(left)} + {len(right)}")
            return _fetch_payroll_range_resilient(
                client, start, end, left, should_use_cache, depot_id, paycodes, log
            ) + _fetch_payroll_range_resilient(
                client, start, end, right, should_use_cache, depot_id, paycodes, log
            )

        if start >= end:
            raise OptibusError(
                f"Payroll request too large even for minimal range ({iso_date(start)}): "
                f"{payload.get('__text__', '')[:800]}"
            )

        total_days = (end - start).days + 1
        left_days = max(1, total_days // 2)
        left_end = start + timedelta(days=left_days - 1)
        right_start = left_end + timedelta(days=1)
        log(
            "  413 too large. Splitting dates: "
            f"{iso_date(start)}..{iso_date(end)} -> "
            f"{iso_date(start)}..{iso_date(left_end)} + {iso_date(right_start)}..{iso_date(end)}"
        )
        return _fetch_payroll_range_resilient(
            client, start, left_end, driver_ids, should_use_cache, depot_id, paycodes, log
        ) + _fetch_payroll_range_resilient(
            client, right_start, end, driver_ids, should_use_cache, depot_id, paycodes, log
        )

    if isinstance(payload, list):
        return payload

    raise OptibusError(f"Unexpected payroll response type: {type(payload)}")


def _fetch_payroll_range_collecting_errors(
    client: OptibusClient,
    start: date,
    end: date,
    driver_ids: list[str],
    should_use_cache: bool,
    depot_id: Optional[str] = None,
    paycodes: Optional[list[str]] = None,
    driver_chunk_index: int = 1,
    driver_chunk_count: int = 1,
    log=print,
) -> tuple[list[dict], list[PayrollTestError], int]:
    """Fetch one payroll range and return rows plus any captured errors."""
    params: dict[str, Any] = {
        "startDate": iso_date(start),
        "endDate": iso_date(end),
        "shouldUseCache": "true" if should_use_cache else "false",
    }

    if paycodes:
        params["paycodes"] = ",".join(str(code) for code in paycodes)

    if driver_ids:
        params["driverIds"] = ",".join(str(driver_id) for driver_id in driver_ids)
    else:
        if not depot_id:
            error = PayrollTestError(
                request_start_date=iso_date(start),
                request_end_date=iso_date(end),
                driver_chunk_index=driver_chunk_index,
                driver_chunk_count=driver_chunk_count,
                driver_count=0,
                driver_ids_preview="",
                should_use_cache=should_use_cache,
                paycodes=",".join(paycodes or []),
                status_code=None,
                error_message="depot_id is required when driver_ids is empty for /v2/payroll.",
                response_excerpt="",
            )
            return [], [error], 0
        params["depotId"] = depot_id

    try:
        payload = client.get_json("/v2/payroll", params=params, allow_413=True)
    except OptibusError as exc:
        error = PayrollTestError(
            request_start_date=iso_date(start),
            request_end_date=iso_date(end),
            driver_chunk_index=driver_chunk_index,
            driver_chunk_count=driver_chunk_count,
            driver_count=len(driver_ids),
            driver_ids_preview=_driver_ids_preview(driver_ids),
            should_use_cache=should_use_cache,
            paycodes=",".join(paycodes or []),
            status_code=exc.status_code,
            error_message=str(exc),
            response_excerpt=_response_excerpt(exc.response_text or exc.response_json),
        )
        return [], [error], 0
    except Exception as exc:
        error = PayrollTestError(
            request_start_date=iso_date(start),
            request_end_date=iso_date(end),
            driver_chunk_index=driver_chunk_index,
            driver_chunk_count=driver_chunk_count,
            driver_count=len(driver_ids),
            driver_ids_preview=_driver_ids_preview(driver_ids),
            should_use_cache=should_use_cache,
            paycodes=",".join(paycodes or []),
            status_code=None,
            error_message=str(exc),
            response_excerpt="",
        )
        return [], [error], 0

    if isinstance(payload, dict) and payload.get("__HTTP_413__"):
        if driver_ids and len(driver_ids) > 1:
            mid = len(driver_ids) // 2
            left = driver_ids[:mid]
            right = driver_ids[mid:]
            log(f"  413 too large. Splitting drivers: {len(driver_ids)} -> {len(left)} + {len(right)}")
            left_rows, left_errors, left_successes = _fetch_payroll_range_collecting_errors(
                client, start, end, left, should_use_cache, depot_id, paycodes, driver_chunk_index, driver_chunk_count, log
            )
            right_rows, right_errors, right_successes = _fetch_payroll_range_collecting_errors(
                client, start, end, right, should_use_cache, depot_id, paycodes, driver_chunk_index, driver_chunk_count, log
            )
            return (
                left_rows + right_rows,
                left_errors + right_errors,
                left_successes + right_successes,
            )

        if start < end:
            total_days = (end - start).days + 1
            left_days = max(1, total_days // 2)
            left_end = start + timedelta(days=left_days - 1)
            right_start = left_end + timedelta(days=1)
            log(
                "  413 too large. Splitting dates: "
                f"{iso_date(start)}..{iso_date(end)} -> "
                f"{iso_date(start)}..{iso_date(left_end)} + {iso_date(right_start)}..{iso_date(end)}"
            )
            left_rows, left_errors, left_successes = _fetch_payroll_range_collecting_errors(
                client,
                start,
                left_end,
                driver_ids,
                should_use_cache,
                depot_id,
                paycodes,
                driver_chunk_index,
                driver_chunk_count,
                log,
            )
            right_rows, right_errors, right_successes = _fetch_payroll_range_collecting_errors(
                client,
                right_start,
                end,
                driver_ids,
                should_use_cache,
                depot_id,
                paycodes,
                driver_chunk_index,
                driver_chunk_count,
                log,
            )
            return (
                left_rows + right_rows,
                left_errors + right_errors,
                left_successes + right_successes,
            )

        error = PayrollTestError(
            request_start_date=iso_date(start),
            request_end_date=iso_date(end),
            driver_chunk_index=driver_chunk_index,
            driver_chunk_count=driver_chunk_count,
            driver_count=len(driver_ids),
            driver_ids_preview=_driver_ids_preview(driver_ids),
            should_use_cache=should_use_cache,
            paycodes=",".join(paycodes or []),
            status_code=payload.get("__status__"),
            error_message="Payroll request too large even for the minimal range.",
            response_excerpt=_response_excerpt(payload.get("__text__") or payload.get("__json__")),
        )
        return [], [error], 0

    if isinstance(payload, list):
        return payload, [], 1

    error = PayrollTestError(
        request_start_date=iso_date(start),
        request_end_date=iso_date(end),
        driver_chunk_index=driver_chunk_index,
        driver_chunk_count=driver_chunk_count,
        driver_count=len(driver_ids),
        driver_ids_preview=_driver_ids_preview(driver_ids),
        should_use_cache=should_use_cache,
        paycodes=",".join(paycodes or []),
        status_code=None,
        error_message=f"Unexpected payroll response type: {type(payload)}",
        response_excerpt=_response_excerpt(payload),
    )
    return [], [error], 0


def fetch_absences(client: OptibusClient, start: date, end: date) -> list[dict]:
    """Fetch paginated driver absences for the date range."""
    page = 1
    all_rows: list[dict] = []
    while True:
        payload = client.get_json(
            "/v2/drivers/absences",
            params={"fromDate": iso_date(start), "toDate": iso_date(end), "page": page},
        )
        absences = payload.get("absences", []) if isinstance(payload, dict) else []
        all_rows.extend(absences)
        pagination = payload.get("pagination", {}) if isinstance(payload, dict) else {}
        current_page = pagination.get("currentPage", page)
        total_pages = pagination.get("totalPages", current_page)
        if current_page >= total_pages:
            break
        page += 1
    return all_rows


def fetch_operational_plan_v2(
    client: OptibusClient,
    start: date,
    end: date,
    depot_uuids: Optional[str | list[str]] = None,
) -> Any:
    """Fetch operational plan data including actual and planned assignments."""
    params: dict[str, Any] = {
        "fromDate": iso_date(start),
        "toDate": iso_date(end),
        "includeStops": "false",
        "includeUnassigned": "false",
    }
    if depot_uuids:
        if isinstance(depot_uuids, (list, tuple, set)):
            params["depotUuids"] = ",".join([str(value) for value in depot_uuids if str(value)])
        else:
            params["depotUuids"] = str(depot_uuids)

    return client.get_json("/v2/operational-plan", params=params)


def ensure_list_payload(payload: Any) -> list[dict]:
    """Normalize an operational-plan response into a list of depot plans."""
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return payload
    return []


def _task_display(task: dict) -> str:
    """Choose the most readable task identifier for allocation exports."""
    display_id = safe_str(task.get("displayId") or "")
    if display_id:
        return display_id
    description = safe_str(task.get("description") or "")
    if description:
        return description
    task_type = safe_str(task.get("type") or task.get("dutyType") or "task")
    task_id = safe_str(task.get("id") or "")
    return f"{task_type}:{task_id}" if task_id else task_type


def build_allocation_maps_from_operational_plan(
    depot_plan: dict,
    by_uuid: dict[str, DriverInfo],
) -> tuple[dict[tuple[str, str], list[str]], dict[tuple[str, str], list[str]]]:
    """Return actual and planned allocation maps keyed by (external_driver_id, date)."""

    def normalize_driver_id(raw: Any) -> str:
        value = safe_str(raw).strip()
        if not value:
            return ""
        if value.isdigit():
            return value
        info = by_uuid.get(value)
        return info.external_id if info else value

    tasks = depot_plan.get("tasks", []) or []
    tasks_by_id: dict[str, str] = {}
    for task in tasks:
        task_id = safe_str(task.get("id"))
        if task_id:
            tasks_by_id[task_id] = _task_display(task)

    actual_map: dict[tuple[str, str], list[str]] = defaultdict(list)
    planned_map: dict[tuple[str, str], list[str]] = defaultdict(list)

    for assignment in depot_plan.get("assignments", []) or []:
        date_text = safe_str(assignment.get("date"))
        if not date_text:
            continue

        for actual in assignment.get("driverAssignments", []) or []:
            driver_id = normalize_driver_id(actual.get("driver") or actual.get("driverId") or "")
            if not driver_id:
                continue
            for task_id in actual.get("assignments", []) or []:
                task_key = safe_str(task_id)
                if task_key:
                    actual_map[(driver_id, date_text)].append(tasks_by_id.get(task_key, task_key))

        for planned in assignment.get("plannedAssignments", []) or []:
            driver_id = normalize_driver_id(planned.get("driver") or planned.get("driverId") or "")
            if not driver_id:
                continue
            for task_id in planned.get("assignments", []) or []:
                task_key = safe_str(task_id)
                if task_key:
                    planned_map[(driver_id, date_text)].append(tasks_by_id.get(task_key, task_key))

    return actual_map, planned_map
