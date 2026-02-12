from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, g, jsonify, request

import db as storage


def create_reservations_blueprint(
    *,
    login_required,
    admin_required,
    db_conn,
    json_error,
    parse_local_datetime,
    home_zone,
    to_utc_iso,
    utc_iso_to_local_iso,
    effective_home_timezone_name,
    effective_user_show_closed_default,
    reservation_payload,
    reservation_to_event_payload,
    validate_reservation_fields,
    can_edit_reservation,
    can_request_change,
    can_reopen_reservation,
    utc_now,
    default_parked_icao: str,
):
    bp = Blueprint("reservation_routes", __name__)

    def _parse_range_window():
        start_raw = (request.args.get("start") or "").strip()
        end_raw = (request.args.get("end") or "").strip()
        if not start_raw or not end_raw:
            return None, None, {"message": "start and end query params are required.", "code": "invalid_range", "status": 400}
        try:
            start_dt = datetime.fromisoformat(start_raw)
            end_dt = datetime.fromisoformat(end_raw)
        except Exception:
            return None, None, {"message": "start and end must be ISO date/datetime values.", "code": "invalid_range", "status": 400}
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=home_zone())
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=home_zone())
        if end_dt <= start_dt:
            return None, None, {"message": "end must be after start.", "code": "invalid_range", "status": 400}
        return to_utc_iso(start_dt), to_utc_iso(end_dt), None

    @bp.get("/api/my-flights", endpoint="api_my_flights")
    @login_required
    def api_my_flights():
        from_utc = to_utc_iso(datetime.now(home_zone()))
        with db_conn() as conn:
            result = storage.list_my_flights(conn, user_id=int(g.current_user["id"]), from_utc=from_utc)
        return jsonify(
            {
                "pending": [reservation_payload(row) for row in result["pending"]],
                "approved_upcoming": [reservation_payload(row) for row in result["approved_upcoming"]],
                "show_closed_default": effective_user_show_closed_default(),
            }
        )

    @bp.get("/api/my-change-requests", endpoint="api_my_change_requests")
    @login_required
    def api_my_change_requests():
        with db_conn() as conn:
            rows = storage.list_my_change_requests(conn, int(g.current_user["id"]))
        payload = []
        for row in rows:
            payload.append(
                {
                    "id": int(row["id"]),
                    "reservation_id": int(row["reservation_id"]),
                    "status": row["status"],
                    "proposed_start": utc_iso_to_local_iso(row["proposed_start_utc"]),
                    "proposed_end": utc_iso_to_local_iso(row["proposed_end_utc"]),
                    "proposed_dep_icao": row["proposed_dep_icao"],
                    "proposed_dest_icao": row["proposed_dest_icao"],
                    "proposed_notes": row["proposed_notes"] or "",
                    "created_at": utc_iso_to_local_iso(row["created_at_utc"]),
                    "reservation_status": row["reservation_status"],
                }
            )
        return jsonify(payload)

    @bp.get("/api/reservations", endpoint="api_reservations")
    @login_required
    def api_reservations():
        start_utc, end_utc, err = _parse_range_window()
        if err:
            return json_error(err["message"], err["status"], err["code"])
        include_nonblocking = str(request.args.get("include_nonblocking") or "").lower() in ("1", "true", "yes", "on")
        with db_conn() as conn:
            rows = storage.list_reservations(conn, start_utc=start_utc, end_utc=end_utc, include_nonblocking=include_nonblocking)
        user = getattr(g, "current_user", None)
        return jsonify([reservation_to_event_payload(row, user) for row in rows])

    @bp.get("/api/pending-reservations", endpoint="api_pending_reservations")
    @admin_required
    def api_pending_reservations():
        with db_conn() as conn:
            rows = storage.list_pending(conn)
        payload = []
        for row in rows:
            payload.append(
                {
                    "id": int(row["id"]),
                    "status": row["status"],
                    "start": utc_iso_to_local_iso(row["start_utc"]),
                    "end": utc_iso_to_local_iso(row["end_utc"]),
                    "dep_icao": row["dep_icao"],
                    "dest_icao": row["dest_icao"],
                    "parked_icao": row["parked_icao"],
                    "traveling_owner": row["traveling_owner_name"],
                    "requested_by": row["requested_by_name"],
                    "notes": row["notes"] or "",
                }
            )
        return jsonify(payload)

    @bp.get("/api/my-pending-reservations", endpoint="api_my_pending_reservations")
    @login_required
    def api_my_pending_reservations():
        with db_conn() as conn:
            rows = conn.execute(
                """
                SELECT r.*, tu.name AS traveling_owner_name
                FROM reservations r
                JOIN users tu ON tu.id = r.traveling_user_id
                WHERE r.status = 'pending'
                  AND r.requested_by_user_id = ?
                ORDER BY r.start_utc ASC
                """,
                (int(g.current_user["id"]),),
            ).fetchall()

        payload = []
        for row in rows:
            payload.append(
                {
                    "id": int(row["id"]),
                    "status": row["status"],
                    "start": utc_iso_to_local_iso(row["start_utc"]),
                    "end": utc_iso_to_local_iso(row["end_utc"]),
                    "dep_icao": row["dep_icao"],
                    "dest_icao": row["dest_icao"],
                    "parked_icao": row["parked_icao"],
                    "traveling_owner": row["traveling_owner_name"],
                    "notes": row["notes"] or "",
                }
            )
        return jsonify(payload)

    @bp.post("/api/reservation-requests", endpoint="api_create_reservation_request")
    @login_required
    def api_create_reservation_request():
        data = request.get_json(silent=True) or {}
        current_user = g.current_user
        normalized, err = validate_reservation_fields(data, current_user=current_user)
        if err:
            return json_error(err["message"], err["status"], err["code"], err.get("field"))
        with db_conn() as conn:
            if not storage.get_user_by_id(conn, int(normalized["traveling_user_id"])):
                return json_error("Traveling owner does not exist.", 400, "invalid_traveling_owner", "traveling_user_id")
            if storage.overlap_exists(conn, start_utc=normalized["start_utc"], end_utc=normalized["end_utc"]):
                return json_error("Requested time overlaps an existing reservation.", 409, "overlap_conflict")
            row = storage.create_reservation(
                conn,
                status="pending",
                start_utc=normalized["start_utc"],
                end_utc=normalized["end_utc"],
                dep_icao=normalized["dep_icao"],
                dest_icao=normalized["dest_icao"],
                parked_icao=normalized["parked_icao"],
                traveling_user_id=int(normalized["traveling_user_id"]),
                requested_by_user_id=int(current_user["id"]),
                notes=normalized["notes"],
            )
        return jsonify({"ok": True, "reservation": reservation_to_event_payload(row, current_user)}), 201

    @bp.patch("/api/reservations/<int:reservation_id>", endpoint="api_update_reservation")
    @login_required
    def api_update_reservation(reservation_id: int):
        data = request.get_json(silent=True) or {}
        with db_conn() as conn:
            row = storage.get_reservation_by_id(conn, reservation_id)
            if not row:
                return json_error("Reservation not found.", 404, "not_found")
            current_user = g.current_user
            if not can_edit_reservation(current_user, row):
                return json_error("You are not allowed to edit this reservation.", 403, "forbidden")

            existing = {
                "start": utc_iso_to_local_iso(row["start_utc"]),
                "end": utc_iso_to_local_iso(row["end_utc"]),
                "dep_icao": row["dep_icao"],
                "dest_icao": row["dest_icao"],
                "parked_icao": row["parked_icao"],
                "traveling_user_id": int(row["traveling_user_id"]),
                "notes": row["notes"] or "",
            }
            normalized, err = validate_reservation_fields(data, existing=existing, current_user=current_user)
            if err:
                return json_error(err["message"], err["status"], err["code"], err.get("field"))
            if not storage.get_user_by_id(conn, int(normalized["traveling_user_id"])):
                return json_error("Traveling owner does not exist.", 400, "invalid_traveling_owner", "traveling_user_id")
            status = row["status"]
            if status in storage.BLOCKING_STATUSES and storage.overlap_exists(
                conn,
                start_utc=normalized["start_utc"],
                end_utc=normalized["end_utc"],
                exclude_id=reservation_id,
            ):
                return json_error("Updated time overlaps an existing reservation.", 409, "overlap_conflict")
            updated = storage.update_reservation_fields(
                conn,
                reservation_id,
                {
                    "start_utc": normalized["start_utc"],
                    "end_utc": normalized["end_utc"],
                    "dep_icao": normalized["dep_icao"],
                    "dest_icao": normalized["dest_icao"],
                    "parked_icao": normalized["parked_icao"],
                    "traveling_user_id": int(normalized["traveling_user_id"]),
                    "notes": normalized["notes"],
                },
            )
        return jsonify({"ok": True, "reservation": reservation_to_event_payload(updated, g.current_user)})

    @bp.post("/api/reservations/<int:reservation_id>/change-requests", endpoint="api_create_change_request")
    @login_required
    def api_create_change_request(reservation_id: int):
        data = request.get_json(silent=True) or {}
        current_user = g.current_user

        with db_conn() as conn:
            row = storage.get_reservation_by_id(conn, reservation_id)
            if not row:
                return json_error("Reservation not found.", 404, "not_found")
            if not can_request_change(current_user, row):
                return json_error("You cannot request changes for this reservation.", 403, "forbidden")

            existing = {
                "start": utc_iso_to_local_iso(row["start_utc"]),
                "end": utc_iso_to_local_iso(row["end_utc"]),
                "dep_icao": row["dep_icao"],
                "dest_icao": row["dest_icao"],
                "parked_icao": row["parked_icao"],
                "traveling_user_id": int(row["traveling_user_id"]),
                "notes": row["notes"] or "",
            }
            normalized, err = validate_reservation_fields(data, existing=existing, current_user=current_user)
            if err:
                return json_error(err["message"], err["status"], err["code"], err.get("field"))

            created = storage.create_change_request(
                conn,
                reservation_id=reservation_id,
                requested_by_user_id=int(current_user["id"]),
                proposed_start_utc=normalized["start_utc"],
                proposed_end_utc=normalized["end_utc"],
                proposed_dep_icao=normalized["dep_icao"],
                proposed_dest_icao=normalized["dest_icao"],
                proposed_notes=normalized["notes"],
            )
        return jsonify({"ok": True, "change_request_id": int(created["id"])})

    @bp.get("/api/reservations/<int:reservation_id>/change-requests", endpoint="api_list_change_requests")
    @admin_required
    def api_list_change_requests(reservation_id: int):
        with db_conn() as conn:
            reservation = storage.get_reservation_by_id(conn, reservation_id)
            if not reservation:
                return json_error("Reservation not found.", 404, "not_found")
            rows = storage.list_change_requests_for_reservation(conn, reservation_id)
        payload = []
        for row in rows:
            payload.append(
                {
                    "id": int(row["id"]),
                    "reservation_id": int(row["reservation_id"]),
                    "status": row["status"],
                    "proposed_start": utc_iso_to_local_iso(row["proposed_start_utc"]),
                    "proposed_end": utc_iso_to_local_iso(row["proposed_end_utc"]),
                    "proposed_dep_icao": row["proposed_dep_icao"],
                    "proposed_dest_icao": row["proposed_dest_icao"],
                    "proposed_notes": row["proposed_notes"] or "",
                    "requested_by": row["requested_by_name"],
                    "decision_note": row["decision_note"] or "",
                }
            )
        return jsonify(payload)

    @bp.post("/api/reservations/<int:reservation_id>/cancel", endpoint="api_cancel_reservation")
    @login_required
    def api_cancel_reservation(reservation_id: int):
        with db_conn() as conn:
            row = storage.get_reservation_by_id(conn, reservation_id)
            if not row:
                return json_error("Reservation not found.", 404, "not_found")
            current_user = g.current_user
            if current_user["role"] != "admin":
                if int(row["requested_by_user_id"]) != int(current_user["id"]) or row["status"] != "pending":
                    return json_error("You are not allowed to cancel this reservation.", 403, "forbidden")
            if row["status"] in ("denied", "canceled"):
                return json_error("Reservation is already closed.", 400, "invalid_status")
            updated = storage.update_reservation_fields(conn, reservation_id, {"status": "canceled"})
        return jsonify({"ok": True, "reservation": reservation_to_event_payload(updated, g.current_user)})

    @bp.post("/api/reservations/<int:reservation_id>/reopen", endpoint="api_reopen_reservation")
    @admin_required
    def api_reopen_reservation(reservation_id: int):
        data = request.get_json(silent=True) or {}
        target_status = str(data.get("target_status") or "pending").strip().lower()
        if target_status != "pending":
            return json_error("target_status must be 'pending' in this release.", 400, "invalid_status", "target_status")

        with db_conn() as conn:
            row = storage.get_reservation_by_id(conn, reservation_id)
            if not row:
                return json_error("Reservation not found.", 404, "not_found")
            if not can_reopen_reservation(g.current_user, row):
                return json_error("Only denied/canceled reservations can be reopened.", 400, "invalid_status")

            if storage.overlap_exists(conn, start_utc=row["start_utc"], end_utc=row["end_utc"], exclude_id=reservation_id):
                return json_error("Cannot reopen because this reservation now overlaps another blocking reservation.", 409, "overlap_conflict")

            updated = storage.update_reservation_fields(
                conn,
                reservation_id,
                {
                    "status": "pending",
                    "approved_by_user_id": None,
                    "decision_at_utc": utc_now().isoformat(),
                },
            )
        return jsonify({"ok": True, "reservation": reservation_to_event_payload(updated, g.current_user)})

    @bp.post("/api/reservations/<int:reservation_id>/change-requests/<int:request_id>/approve", endpoint="api_approve_change_request")
    @admin_required
    def api_approve_change_request(reservation_id: int, request_id: int):
        with db_conn() as conn:
            reservation = storage.get_reservation_by_id(conn, reservation_id)
            if not reservation:
                return json_error("Reservation not found.", 404, "not_found")
            req = storage.get_change_request_by_id(conn, request_id)
            if not req or int(req["reservation_id"]) != reservation_id:
                return json_error("Change request not found.", 404, "not_found")
            if req["status"] != "pending":
                return json_error("Only pending change requests can be approved.", 400, "invalid_status")
            if reservation["status"] != "approved":
                return json_error("Target reservation is no longer approved.", 400, "invalid_status")

            if storage.overlap_exists(
                conn,
                start_utc=req["proposed_start_utc"],
                end_utc=req["proposed_end_utc"],
                exclude_id=reservation_id,
            ):
                return json_error("Change request overlaps another blocking reservation.", 409, "overlap_conflict")

            storage.update_reservation_fields(
                conn,
                reservation_id,
                {
                    "start_utc": req["proposed_start_utc"],
                    "end_utc": req["proposed_end_utc"],
                    "dep_icao": req["proposed_dep_icao"],
                    "dest_icao": req["proposed_dest_icao"],
                    "parked_icao": req["proposed_dest_icao"],
                    "notes": req["proposed_notes"] or reservation["notes"] or "",
                },
            )
            storage.update_change_request_fields(
                conn,
                request_id,
                {
                    "status": "applied",
                    "decided_by_user_id": int(g.current_user["id"]),
                    "decision_note": "Approved and applied.",
                    "decided_at_utc": utc_now().isoformat(),
                },
            )
        return jsonify({"ok": True})

    @bp.post("/api/reservations/<int:reservation_id>/change-requests/<int:request_id>/deny", endpoint="api_deny_change_request")
    @admin_required
    def api_deny_change_request(reservation_id: int, request_id: int):
        data = request.get_json(silent=True) or {}
        note = str(data.get("note") or "").strip()
        with db_conn() as conn:
            reservation = storage.get_reservation_by_id(conn, reservation_id)
            if not reservation:
                return json_error("Reservation not found.", 404, "not_found")
            req = storage.get_change_request_by_id(conn, request_id)
            if not req or int(req["reservation_id"]) != reservation_id:
                return json_error("Change request not found.", 404, "not_found")
            if req["status"] != "pending":
                return json_error("Only pending change requests can be denied.", 400, "invalid_status")
            storage.update_change_request_fields(
                conn,
                request_id,
                {
                    "status": "denied",
                    "decided_by_user_id": int(g.current_user["id"]),
                    "decision_note": note or "Denied by admin.",
                    "decided_at_utc": utc_now().isoformat(),
                },
            )
        return jsonify({"ok": True})

    @bp.post("/api/reservations/<int:reservation_id>/approve", endpoint="api_approve_reservation")
    @admin_required
    def api_approve_reservation(reservation_id: int):
        with db_conn() as conn:
            row = storage.get_reservation_by_id(conn, reservation_id)
            if not row:
                return json_error("Reservation not found.", 404, "not_found")
            if row["status"] != "pending":
                return json_error("Only pending reservations can be approved.", 400, "invalid_status")
            if storage.overlap_exists(conn, start_utc=row["start_utc"], end_utc=row["end_utc"], exclude_id=reservation_id):
                return json_error("Cannot approve because this request now overlaps another reservation.", 409, "overlap_conflict")
            updated = storage.update_reservation_fields(
                conn,
                reservation_id,
                {
                    "status": "approved",
                    "approved_by_user_id": int(g.current_user["id"]),
                    "decision_at_utc": utc_now().isoformat(),
                },
            )
        return jsonify({"ok": True, "reservation": reservation_to_event_payload(updated, g.current_user)})

    @bp.post("/api/reservations/<int:reservation_id>/deny", endpoint="api_deny_reservation")
    @admin_required
    def api_deny_reservation(reservation_id: int):
        data = request.get_json(silent=True) or {}
        note = str(data.get("note") or "").strip()
        with db_conn() as conn:
            row = storage.get_reservation_by_id(conn, reservation_id)
            if not row:
                return json_error("Reservation not found.", 404, "not_found")
            if row["status"] != "pending":
                return json_error("Only pending reservations can be denied.", 400, "invalid_status")
            fields = {
                "status": "denied",
                "approved_by_user_id": int(g.current_user["id"]),
                "decision_at_utc": utc_now().isoformat(),
            }
            if note:
                fields["notes"] = f"{(row['notes'] or '').strip()}\\nDenied: {note}".strip()
            updated = storage.update_reservation_fields(conn, reservation_id, fields)
        return jsonify({"ok": True, "reservation": reservation_to_event_payload(updated, g.current_user)})

    @bp.get("/api/availability-summary", endpoint="api_availability_summary")
    @login_required
    def api_availability_summary():
        from_raw = (request.args.get("from") or "").strip()
        to_raw = (request.args.get("to") or "").strip()
        now_local = datetime.now(home_zone())

        if from_raw:
            parsed_from = parse_local_datetime(from_raw)
            if not parsed_from:
                return json_error("Invalid from value.", 400, "invalid_range", "from")
        else:
            parsed_from = now_local
        if to_raw:
            parsed_to = parse_local_datetime(to_raw)
            if not parsed_to:
                return json_error("Invalid to value.", 400, "invalid_range", "to")
        else:
            parsed_to = parsed_from + timedelta(days=30)
        if parsed_to <= parsed_from:
            return json_error("to must be after from.", 400, "invalid_range", "to")

        from_utc = to_utc_iso(parsed_from)
        to_utc = to_utc_iso(parsed_to)
        now_utc = to_utc_iso(now_local)
        with db_conn() as conn:
            last = storage.last_approved_before(conn, now_utc=now_utc)
            parked = last["parked_icao"] if last else (default_parked_icao or "Unknown")
            next_available = storage.find_next_available_window(conn, from_utc=from_utc, to_utc=to_utc)
            upcoming_rows = storage.list_upcoming(conn, from_utc=from_utc, limit=8)

        upcoming = []
        for row in upcoming_rows:
            upcoming.append(
                {
                    "id": int(row["id"]),
                    "status": row["status"],
                    "start": utc_iso_to_local_iso(row["start_utc"]),
                    "end": utc_iso_to_local_iso(row["end_utc"]),
                    "dep_icao": row["dep_icao"],
                    "dest_icao": row["dest_icao"],
                    "parked_icao": row["parked_icao"],
                    "traveling_owner": row["traveling_owner_name"],
                }
            )

        return jsonify(
            {
                "current_parked_icao": parked,
                "next_available": (
                    {
                        "start": utc_iso_to_local_iso(next_available["start_utc"]),
                        "end": utc_iso_to_local_iso(next_available["end_utc"]),
                    }
                    if next_available
                    else None
                ),
                "upcoming": upcoming,
                "timezone": effective_home_timezone_name(),
            }
        )

    return bp
