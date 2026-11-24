#!/usr/bin/env python3
"""End-to-end demo: seed segments, issue a report, then run consecutive corrections.

The correction sequence demonstrates that each receipt reflects only its own
operation: an in-window change marks the report pending_recalc, an
out-of-window addition does not re-list it, and reverting the in-window value
to the original still lists the report (which stays pending_recalc).

Usage (service already running, e.g. via docker compose):

    python scripts/demo.py --base-url http://127.0.0.1:8000

The same calls can be made from Swagger UI at ``/docs``; this script prints
each request/response pair so the numbers can be checked by hand.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta

import httpx

RETROFIT = date(2026, 3, 10)
BEFORE = [RETROFIT - timedelta(days=i) for i in range(1, 8)]
AFTER = [RETROFIT + timedelta(days=i) for i in range(1, 8)]


def _readings(before: float, after: float) -> dict:
    return {
        "readings": [
            {"date": d.isoformat(), "kwh_per_lamp": before} for d in BEFORE
        ]
        + [{"date": d.isoformat(), "kwh_per_lamp": after} for d in AFTER]
    }


def _show(label: str, payload: dict) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--suffix", default=str(int(time.time())), help="unique suffix for names"
    )
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    suffix = args.suffix

    with httpx.Client(base_url=base, timeout=20.0) as client:
        health = client.get("/healthz")
        health.raise_for_status()
        print("health:", health.json())

        def create(name: str, retrofit: date | None) -> int:
            resp = client.post(
                "/api/segments",
                json={
                    "name": f"{name}-{suffix}",
                    "retrofit_date": retrofit.isoformat() if retrofit else None,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            print(f"created segment {body['id']}: {body['name']} retrofit={body['retrofit_date']}")
            return body["id"]

        target_id = create("目标路段", RETROFIT)
        control1 = create("对照路段一", None)
        control2 = create("对照路段二", None)
        future = create("对照路段三-后段末日后换灯", RETROFIT + timedelta(days=8))
        incomplete = create("缺数对照-应被排除", None)

        target_values = _readings(10.0, 8.0)
        client.put(f"/api/segments/{target_id}/readings", json=target_values).raise_for_status()
        client.put(f"/api/segments/{control1}/readings", json=_readings(10.2, 9.7)).raise_for_status()
        client.put(f"/api/segments/{control2}/readings", json=_readings(10.4, 9.9)).raise_for_status()
        client.put(f"/api/segments/{future}/readings", json=_readings(10.2, 9.7)).raise_for_status()
        partial = _readings(7.0, 6.0)
        partial["readings"] = partial["readings"][:-1]  # gap in the after window
        client.put(f"/api/segments/{incomplete}/readings", json=partial).raise_for_status()

        issued = client.post("/api/reports", params={"segment_id": target_id})
        issued.raise_for_status()
        body = issued.json()
        _show("report issuance", body)
        if body["status"] != "issued":
            print("UNEXPECTED: report deferred", file=sys.stderr)
            return 1
        report = body["report"]
        print(
            "\nnet savings kWh/lamp/day:",
            report["net_savings_per_lamp_per_day"],
            "(target reduction",
            report["target"]["reduction"],
            "- control average",
            report["control_avg_reduction"],
            ")",
        )

        corrected = client.put(
            f"/api/segments/{control1}/readings",
            json={"readings": [{"date": "2026-03-13", "kwh_per_lamp": 9.0}]},
        )
        corrected.raise_for_status()
        _show("correction 1: control1 in-window 9.7 -> 9.0", corrected.json())

        detail = client.get(f"/api/reports/{report['id']}")
        detail.raise_for_status()
        after = detail.json()
        _show("report after correction 1", after)
        assert after["status"] == "pending_recalc", after["status"]
        assert after["net_savings_per_lamp_per_day"] == report[
            "net_savings_per_lamp_per_day"
        ]
        assert after["stale_reasons"], "missing stale reasons"
        print(
            "\nOK: report is pending_recalc, original result still readable, "
            f"stale segment ids={after['stale_segment_ids']}"
        )

        # Consecutive corrections: the receipt must reflect only each operation.
        out_of_window = client.put(
            f"/api/segments/{control1}/readings",
            json={"readings": [{"date": "2026-04-01", "kwh_per_lamp": 3.3}]},
        )
        out_of_window.raise_for_status()
        _show("correction 2: control1 out-of-window add 2026-04-01", out_of_window.json())
        assert out_of_window.json()["affected_reports"] == [], out_of_window.json()
        print("OK: out-of-window correction does not re-list the pending report")

        reverted = client.put(
            f"/api/segments/{control1}/readings",
            json={"readings": [{"date": "2026-03-13", "kwh_per_lamp": 9.7}]},
        )
        reverted.raise_for_status()
        _show("correction 3: control1 in-window back to original 9.7", reverted.json())
        assert reverted.json()["affected_reports"] == [
            {
                "report_id": report["id"],
                "previous_status": "pending_recalc",
                "status": "pending_recalc",
            }
        ], reverted.json()
        still = client.get(f"/api/reports/{report['id']}").json()
        assert still["status"] == "pending_recalc", still["status"]
        print("OK: reverting to the original value still lists the report; it stays pending_recalc")

        listing = client.get("/api/reports", params={"status": "pending_recalc"})
        listing.raise_for_status()
        print("pending_recalc reports:", [r["id"] for r in listing.json()])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
