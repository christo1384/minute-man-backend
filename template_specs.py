"""
Minute Man v5 — builtin TemplateSpecs.

The two builtin templates ("safety" and "general") become rows in the new
`templates` table so the front-end has ONE list of templates, but they KEEP
their dedicated prompt + export paths (the hand-tuned v4 behaviour, which is
regression-tested to stay byte-comparable). These specs describe their
structure for the UI and for anything that wants to reason about them.

Used by: the 0002 migration (seeding), db.py fresh-install seeding, and
main.py (/api/templates list).
"""

SPEC_VERSION = 1

SAFETY_SPEC = {
    "version": SPEC_VERSION,
    "sections": [
        {"kind": "summary", "title": "Summary", "fields": [
            {"key": "meeting_type", "label": "Meeting Type"},
            {"key": "site_name", "label": "Site"},
            {"key": "meeting_date", "label": "Date"},
            {"key": "led_by", "label": "Led By"},
        ]},
        {"kind": "incidents", "title": "Incidents Reviewed", "columns": [
            {"maps_to": "description", "label": "Description"},
            {"maps_to": "severity", "label": "Severity"},
            {"maps_to": "outcome", "label": "Review Outcome"},
        ]},
        {"kind": "hazards", "title": "Hazards & Controls", "columns": [
            {"maps_to": "hazard", "label": "Hazard Identified"},
            {"maps_to": "control", "label": "Control Discussed"},
            {"maps_to": "control_tier", "label": "Hierarchy of Controls Tier"},
            {"maps_to": "compliance_note", "label": "HSWA Compliance Note"},
        ]},
        {"kind": "actions", "title": "Action Register", "columns": [
            {"maps_to": "who", "label": "Who"},
            {"maps_to": "what", "label": "What"},
            {"maps_to": "by_when", "label": "By When"},
        ]},
        {"kind": "decisions", "title": "Decisions", "columns": [
            {"maps_to": "decision", "label": "Decision"},
        ]},
        {"kind": "attendance", "title": "Attendance Record", "ai": False, "columns": [
            {"maps_to": "name", "label": "Name"},
            {"maps_to": "signature", "label": "Signature"},
        ]},
    ],
}

GENERAL_SPEC = {
    "version": SPEC_VERSION,
    "sections": [
        {"kind": "summary", "title": "Summary", "fields": [
            {"key": "meeting_type", "label": "Meeting Type"},
            {"key": "site_name", "label": "Site"},
            {"key": "meeting_date", "label": "Date"},
            {"key": "led_by", "label": "Led By"},
        ]},
        {"kind": "actions", "title": "Action Register", "columns": [
            {"maps_to": "who", "label": "Who"},
            {"maps_to": "what", "label": "What"},
            {"maps_to": "by_when", "label": "By When"},
        ]},
        {"kind": "decisions", "title": "Decisions", "columns": [
            {"maps_to": "decision", "label": "Decision"},
        ]},
        {"kind": "attendance", "title": "Attendees", "ai": False, "columns": [
            {"maps_to": "name", "label": "Name"},
            {"maps_to": "signature", "label": "Signature"},
        ]},
    ],
}

# (name, builtin_key, spec) — builtin_key lands in extra so /api/minutes can
# route builtin template_ids to the dedicated v4 code paths.
BUILTIN_TEMPLATES = [
    ("Safety / Toolbox Talk", "safety", SAFETY_SPEC),
    ("General Meeting", "general", GENERAL_SPEC),
]
