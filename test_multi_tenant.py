#!/usr/bin/env python3
"""
Multi-tenant isolation integration test.

Usage (backend must be running at localhost:5002):
    python test_multi_tenant.py
"""

import sys
import requests

BASE = "http://localhost:5002"


def get_default_key():
    """Retrieve the default admin API key from the backend."""
    r = requests.get(f"{BASE}/api/system/settings/api-key")
    r.raise_for_status()
    return r.json()["api_key"]


def headers(api_key):
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def check(condition, label):
    status = "✅" if condition else "❌"
    print(f"  {status}  {label}")
    if not condition:
        sys.exit(1)


def main():
    print("=== Multi-Tenant Isolation Test ===\n")

    # ---- 1. Get default admin key ----------------------------------------
    admin_key = get_default_key()
    print(f"Admin key: {admin_key[:20]}…")

    # ---- 2. Create tenant-2 via admin API --------------------------------
    r = requests.post(
        f"{BASE}/api/admin/tenants",
        json={"name": "Tenant B", "slug": "tenant-b"},
        headers=headers(admin_key),
    )
    check(r.status_code == 201, f"Create tenant-B → 201 (got {r.status_code})")
    tenant_b_key = r.json()["first_api_key"]
    tenant_b_id = r.json()["tenant"]["id"]
    print(f"Tenant B id={tenant_b_id}, key={tenant_b_key[:20]}…")

    # ---- 3. Upload a document as Tenant A (default) ----------------------
    with open("/tmp/tenant_a_test.txt", "w") as f:
        f.write("This is a document belonging to Tenant A.")

    r = requests.post(
        f"{BASE}/api/upload",
        files={"file": ("tenant_a_doc.txt", open("/tmp/tenant_a_test.txt", "rb"), "text/plain")},
        data={"document_type": "policy"},
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    check(r.status_code == 201, f"Tenant A upload → 201 (got {r.status_code})")
    doc_a_id = r.json()["document_id"]
    print(f"Tenant A document id={doc_a_id}")

    # ---- 4. Upload a document as Tenant B --------------------------------
    with open("/tmp/tenant_b_test.txt", "w") as f:
        f.write("This is a document belonging to Tenant B.")

    r = requests.post(
        f"{BASE}/api/upload",
        files={"file": ("tenant_b_doc.txt", open("/tmp/tenant_b_test.txt", "rb"), "text/plain")},
        data={"document_type": "policy"},
        headers={"Authorization": f"Bearer {tenant_b_key}"},
    )
    check(r.status_code == 201, f"Tenant B upload → 201 (got {r.status_code})")
    doc_b_id = r.json()["document_id"]
    print(f"Tenant B document id={doc_b_id}")

    # ---- 5. Tenant A should NOT see Tenant B's document ------------------
    r = requests.get(f"{BASE}/api/documents", headers=headers(admin_key))
    r.raise_for_status()
    doc_ids_a = [d["id"] for d in r.json()["documents"]]
    check(doc_b_id not in doc_ids_a, "Tenant A does NOT see Tenant B's document")
    check(doc_a_id in doc_ids_a, "Tenant A DOES see its own document")

    # ---- 6. Tenant B should NOT see Tenant A's document ------------------
    r = requests.get(f"{BASE}/api/documents", headers=headers(tenant_b_key))
    r.raise_for_status()
    doc_ids_b = [d["id"] for d in r.json()["documents"]]
    check(doc_a_id not in doc_ids_b, "Tenant B does NOT see Tenant A's document")
    check(doc_b_id in doc_ids_b, "Tenant B DOES see its own document")

    # ---- 7. Tenant B cannot fetch Tenant A's document by ID --------------
    r = requests.get(f"{BASE}/api/documents/{doc_a_id}", headers=headers(tenant_b_key))
    check(r.status_code == 404, f"Tenant B GET /documents/{doc_a_id} → 404 (got {r.status_code})")

    # ---- 8. KB stats isolation -------------------------------------------
    # Save Tenant A's doc to KB
    import time; time.sleep(2)  # brief wait for analysis
    r = requests.post(f"{BASE}/api/documents/{doc_a_id}/save", headers=headers(admin_key))
    # May fail if still processing — that's ok for this test; at minimum KB should be separate
    r_stats_b = requests.get(f"{BASE}/api/kb/stats", headers=headers(tenant_b_key))
    r_stats_b.raise_for_status()
    check(
        r_stats_b.json().get("total_chunks", 0) == 0,
        "Tenant B KB is empty (not polluted by Tenant A's data)",
    )

    print("\n🎉 All isolation checks passed!")


if __name__ == "__main__":
    main()
