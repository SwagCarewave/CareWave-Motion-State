from __future__ import annotations

from app.services.csv_reader import load_csi_csv


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def csv_packets(sample_csv, limit: int | None = None):
    table = load_csi_csv(sample_csv)
    out = []
    for t, rx, amp in table.packets():
        out.append({"ts": table.origin_ts + t, "rx": rx, "amplitude": amp.tolist()})
    return out[:limit] if limit else out


def send_packets(client, session_id, packets, size=300):
    totals = {"accepted": 0, "rejected_invalid": 0, "rejected_incomplete": 0, "rejected_stale": 0, "frames": 0}
    for i in range(0, len(packets), size):
        batch = packets[i:i + size]
        client.clock.now = batch[-1]["ts"]
        res = client.post(f"/api/sessions/{session_id}/packets", json={"packets": batch})
        assert res.status_code == 200, res.text
        for k, v in res.json().items():
            totals[k] += v
    return totals


def start_session(client) -> str:
    res = client.post("/api/sessions")
    assert res.status_code in (200, 201)
    return res.json()["id"]
