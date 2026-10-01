import concurrent.futures

from fastapi.testclient import TestClient

from tests.synthetic import create_manifest, make_chunks, upload_chunks


def test_out_of_order_retransmit_completes_manifest_and_duplicate_upload_is_idempotent(client: TestClient):
    chunks = make_chunks(sample_chunks=(120, 240, 360))
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks, order=[2, 0, 1])
    response = client.put(
        f"/manifests/{manifest['id']}/chunks/1/raw",
        files={"chunk_file": ("1.bin", chunks[1]["raw"], "application/octet-stream")},
    )
    assert response.status_code == 200
    finalized = client.post(f"/manifests/{manifest['id']}/finalize").json()
    assert finalized["completed"] is True
    second = client.post(f"/manifests/{manifest['id']}/finalize").json()
    assert second == {
        "completed": False,
        "already_completed": True,
        "manifest_id": manifest["id"],
    }


def test_missing_block_is_reported_not_sorted_away(client: TestClient):
    chunks = make_chunks(sample_chunks=(120, 240, 360))
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks, order=[2, 0])
    response = client.post(f"/manifests/{manifest['id']}/finalize")
    assert response.status_code == 422
    codes = {issue["code"] for issue in response.json()["detail"]["issues"]}
    assert "missing_chunk" in codes
    refreshed = client.get(f"/manifests/{manifest['id']}").json()
    assert refreshed["status"] == "open"


def test_overlapping_declaration_is_rejected_at_manifest_create(client: TestClient):
    chunks = make_chunks(sample_chunks=(120, 240))
    chunks[1]["byte_offset"] = chunks[0]["byte_offset"]  # overlap instead of next offset
    response = client.post(
        "/manifests",
        json={
            "name": "overlap",
            "expected_chunks": [{k: v for k, v in chunk.items() if k != "raw"} for chunk in chunks],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["issues"][0]["code"] == "byte_range_overlap"


def test_byte_digest_mismatch_blocks_completion(client: TestClient):
    chunks = make_chunks(sample_chunks=(120, 240))
    manifest = create_manifest(client, chunks)
    corrupt = bytearray(chunks[0]["raw"])
    corrupt[10] ^= 0xFF
    response = client.put(
        f"/manifests/{manifest['id']}/chunks/0/raw",
        files={"chunk_file": ("0.bin", bytes(corrupt), "application/octet-stream")},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "byte_digest_mismatch"


def test_concurrent_finalization_succeeds_once(client: TestClient):
    chunks = make_chunks(sample_chunks=(120, 240))
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks)

    def finalize(_):
        return client.post(f"/manifests/{manifest['id']}/finalize").status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(finalize, range(8)))
    bodies = []
    completed = 0
    for response in responses:
        assert response.status_code in {200, 409}
        if response.status_code == 200:
            body = response.json()
            bodies.append(body)
            completed += int(body.get("completed") is True)
    assert completed == 1
    refreshed = client.get(f"/manifests/{manifest['id']}").json()
    assert refreshed["status"] == "completed"
