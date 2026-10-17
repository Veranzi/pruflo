"""Load test at the upload limits (Docs/backend_plan.md, Step 17).

Generates documents at the Step 0 limits when it runs (nothing is stored in
the repository) and pushes them through a running API exactly as the app
does: upload URL, PUT, complete. Then it waits for processing and reports
timings.

    python -m app.loadtest --api http://localhost:8000 --token "dev:uid:admin@acme.com" \\
        --case case_... --files 10 --concurrency 4

Use a case created for the test, and an investigator or admin token (a
Firebase ID token, ``dev:`` locally, or an API key with evidence:write and
cases:read).

Kinds of file, cycled: a 200-page text PDF (the page limit), a PDF just
under 25 MB (the size limit), and a 10,000-row CSV.
"""

import argparse
import io
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx

from app.config import MAX_FILE_BYTES, MAX_PAGES


def pdf_pages(pages: int) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_font("Helvetica", size=11)
    for number in range(1, pages + 1):
        pdf.add_page()
        pdf.cell(0, 8, f"Load test page {number} of {pages}", new_x="LMARGIN", new_y="NEXT")
        for line in range(40):
            pdf.cell(
                0,
                6,
                f"Line {line + 1}: item LT-{number:03d}-{line:02d} qty {line + 1}",
                new_x="LMARGIN",
                new_y="NEXT",
            )
    return bytes(pdf.output())


def pdf_size(target: int) -> bytes:
    """A valid PDF padded with an incompressible image up to ``target`` bytes."""
    import os

    from fpdf import FPDF
    from PIL import Image

    side = int(((target * 0.95) / 3) ** 0.5)
    image = Image.frombytes("RGB", (side, side), os.urandom(side * side * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=0)
    pdf = FPDF()
    pdf.add_page()
    pdf.image(buffer, x=10, y=10, w=190)
    data = bytes(pdf.output())
    if len(data) > target:
        raise SystemExit(f"Generated {len(data)} bytes, over {target}; lower the target.")
    return data


def csv_rows(rows: int) -> bytes:
    lines = ["Item Code,Description,Qty Delivered,UOM"]
    lines += [f"LT-{n:05d},Load test item {n},{n % 50 + 1},pcs" for n in range(1, rows + 1)]
    return ("\n".join(lines) + "\n").encode()


@dataclass
class Result:
    name: str
    size: int
    upload_seconds: float
    status: str
    processed_seconds: float | None = None


def upload_one(client: httpx.Client, case_id: str, name: str, data: bytes, mime: str) -> Result:
    started = time.monotonic()
    slot = client.post(
        f"/v1/cases/{case_id}/evidence/upload-url",
        json={
            "file_name": name,
            "size_bytes": len(data),
            "content_type": mime,
            "evidence_types": ["documents"],
        },
    )
    slot.raise_for_status()
    body = slot.json()
    put = httpx.put(body["upload_url"], content=data, headers=body["headers"], timeout=300)
    put.raise_for_status()
    done = client.post(f"/v1/cases/{case_id}/evidence/{body['evidence_id']}/complete")
    status = done.json().get("status", f"HTTP {done.status_code}")
    return Result(name, len(data), time.monotonic() - started, str(status))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.loadtest")
    parser.add_argument("--api", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--files", type=int, default=6)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--wait", type=int, default=600, help="seconds to wait for processing")
    args = parser.parse_args()

    print("Generating files at the limits...")  # noqa: T201
    kinds = [
        ("pages.pdf", pdf_pages(MAX_PAGES), "application/pdf"),
        ("size.pdf", pdf_size(MAX_FILE_BYTES - 64 * 1024), "application/pdf"),
        ("rows.csv", csv_rows(10_000), "text/csv"),
    ]
    jobs = [
        (f"lt-{n:03d}-{name}", data, mime)
        for n, (name, data, mime) in ((i, kinds[i % len(kinds)]) for i in range(args.files))
    ]
    headers = {"Authorization": f"Bearer {args.token}"}
    with httpx.Client(base_url=args.api, headers=headers, timeout=300) as client:
        started = time.monotonic()
        with ThreadPoolExecutor(args.concurrency) as pool:
            results = list(pool.map(lambda j: upload_one(client, args.case, *j), jobs))
        pending = {r.name: r for r in results}
        while pending and time.monotonic() - started < args.wait:
            items = client.get(f"/v1/cases/{args.case}/evidence", params={"limit": 200}).json()
            for item in items.get("items", []):
                result = pending.get(item["file_name"])
                if result and item["status"] not in ("queued", "extracting", "uploading"):
                    result.status = item["status"]
                    result.processed_seconds = time.monotonic() - started
                    del pending[item["file_name"]]
            time.sleep(2)

    for r in results:
        processed = f"{r.processed_seconds:.1f}s" if r.processed_seconds else "not finished"
        print(
            f"{r.name:28} {r.size / 1e6:6.1f} MB  upload {r.upload_seconds:6.1f}s  "  # noqa: T201
            f"processed {processed:>12}  {r.status}"
        )
    uploads = [r.upload_seconds for r in results]
    print(
        f"\nUploads: median {statistics.median(uploads):.1f}s, max {max(uploads):.1f}s; "  # noqa: T201
        f"{sum(1 for r in results if r.processed_seconds)} of {len(results)} processed."
    )


if __name__ == "__main__":
    main()
