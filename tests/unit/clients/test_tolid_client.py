from __future__ import annotations

from broker.clients.tolid import ToLIDClient


def test_extract_result_immediate_success_shape_uses_species_relationship():
    data = {
        "data": [
            {
                "id": "mMacGis1",
                "type": "specimen",
                "attributes": {
                    "specimen_id": "MG_PC43",
                    "requested_taxonomy_id": 9411,
                },
                "relationships": {
                    "species": {
                        "data": {
                            "id": "9411",
                            "type": "species",
                        }
                    }
                },
            }
        ]
    }

    result = ToLIDClient._extract_result(data, specimen_id="MG_PC43")

    assert result.status == "assigned"
    assert result.tolid == "mMacGis1"
    assert result.request_id is None


def test_extract_result_pending_shape_without_species_relationship():
    data = {
        "data": [
            {
                "id": "10306",
                "type": "request",
                "attributes": {
                    "requested_taxonomy_id": 2792576,
                    "specimen_id": "332a",
                    "status": "Pending",
                },
                "relationships": {
                    "user": {
                        "data": {
                            "id": "242",
                        }
                    }
                },
            }
        ]
    }

    result = ToLIDClient._extract_result(data, specimen_id="332a")

    assert result.status == "pending"
    assert result.request_id == "10306"
    assert result.pending_status == "Pending"
    assert result.tolid is None
