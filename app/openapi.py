"""Hand-authored OpenAPI 3.0 description (keeps the service dependency-free)."""

from __future__ import annotations

from typing import Any


def openapi_spec() -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Local Protective Material Reservation API",
            "version": "1.0.0",
            "description": (
                "Local simulation of protective materials and drill equipment "
                "reservations. Segment mutex rules remain authoritative; resource "
                "allocation additionally prevents overbooking scarce units."
            ),
        },
        "paths": {
            "/healthz": {"get": {"summary": "Liveness", "responses": {"200": {"description": "OK"}}}},
            "/segment-rules": {
                "get": {
                    "summary": "Read the existing segment mutex matrix",
                    "responses": {"200": {"description": "Matrix entries"}},
                }
            },
            "/resources": {
                "get": {"summary": "List SKUs and current status counts", "responses": {"200": {"description": "SKUs"}}},
                "post": {
                    "summary": "Create a resource SKU and one or more tangible units",
                    "requestBody": {"required": True},
                    "responses": {"201": {"description": "Created"}, "400": {"description": "Invalid"}, "409": {"description": "Conflict"}},
                },
            },
            "/resources/{sku}/units": {
                "get": {
                    "summary": "List individual resource units",
                    "parameters": [{"name": "sku", "in": "path", "required": True}],
                    "responses": {"200": {"description": "Units"}, "404": {"description": "Not found"}},
                }
            },
            "/resources/{sku}/expand": {
                "post": {
                    "summary": "Add available units to an existing SKU",
                    "parameters": [{"name": "sku", "in": "path", "required": True}],
                    "responses": {"200": {"description": "Expanded"}, "404": {"description": "Not found"}},
                }
            },
            "/resources/units/{unit_id}/status": {
                "post": {
                    "summary": "Set a unit to available, maintenance, or retired",
                    "parameters": [{"name": "unit_id", "in": "path", "required": True}],
                    "responses": {"200": {"description": "Status set"}, "409": {"description": "Illegal transition or in use"}},
                }
            },
            "/reservations": {
                "get": {"summary": "List reservations and current allocations", "responses": {"200": {"description": "Reservations"}}},
                "post": {
                    "summary": "Declare requirements and atomically reserve a window",
                    "requestBody": {"required": True},
                    "responses": {
                        "200": {"description": "Idempotent replay"},
                        "201": {"description": "Created"},
                        "409": {"description": "Segment conflict or insufficient resources"},
                    },
                },
            },
            "/reservations/{plan_id}": {
                "get": {"summary": "Get one reservation", "responses": {"200": {"description": "Plan"}, "404": {"description": "Not found"}}}
            },
            "/reservations/{plan_id}/readiness": {
                "get": {
                    "summary": "Check every allocated unit before starting work",
                    "responses": {"200": {"description": "Readiness, including each blocked item"}},
                }
            },
            "/reservations/{plan_id}/start": {
                "post": {
                    "summary": "Start a plan; blocked item-by-item if any unit is unavailable",
                    "responses": {"200": {"description": "Started or already started"}, "409": {"description": "Blocked"}},
                }
            },
            "/reservations/{plan_id}/replacements": {
                "post": {
                    "summary": "Replace one failed unit with an available equivalent",
                    "responses": {"200": {"description": "Replaced"}, "404": {"description": "Missing allocation"}, "409": {"description": "No replacement"}},
                }
            },
            "/reservations/{plan_id}/release": {
                "post": {
                    "summary": "Close out and release all allocated units exactly once",
                    "responses": {"200": {"description": "Released; replay is already_released=true"}},
                }
            },
            "/audit-logs": {
                "get": {
                    "summary": "List append-only audit history",
                    "parameters": [{"name": "plan_id", "in": "query", "required": False}],
                    "responses": {"200": {"description": "Audit entries"}},
                }
            },
        },
        "components": {
            "schemas": {
                "Requirement": {
                    "type": "object",
                    "required": ["sku", "quantity"],
                    "properties": {"sku": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1}},
                },
                "ReservationRequest": {
                    "type": "object",
                    "required": ["idempotency_key", "segment", "start_at", "end_at", "requirements"],
                    "properties": {
                        "idempotency_key": {"type": "string"},
                        "plan_id": {"type": "string"},
                        "segment": {"type": "string"},
                        "start_at": {"type": "string", "format": "date-time"},
                        "end_at": {"type": "string", "format": "date-time"},
                        "requirements": {"type": "array", "items": {"$ref": "#/components/schemas/Requirement"}},
                    },
                },
                "UnitStatusRequest": {
                    "type": "object",
                    "required": ["status"],
                    "properties": {
                        "status": {"type": "string", "enum": ["available", "maintenance", "retired"]},
                        "reason": {"type": "string"},
                    },
                },
                "ReplacementRequest": {
                    "type": "object",
                    "required": ["old_unit_id"],
                    "properties": {
                        "old_unit_id": {"type": "string"},
                        "replacement_unit_id": {"type": "string", "nullable": True},
                        "reason": {"type": "string"},
                        "idempotency_key": {"type": "string"},
                    },
                },
                "Error": {
                    "type": "object",
                    "properties": {
                        "error": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "message": {"type": "string"},
                                "details": {"type": "object", "additionalProperties": True},
                            },
                        }
                    },
                },
            }
        },
    }
