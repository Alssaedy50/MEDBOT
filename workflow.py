"""Single-owner workflow state for MEDBOT.

Several admin/student flows wait for a typed message (a section name, a
rejection reason, a notification body, ...). Each used to store its own keys
in `context.user_data` and every text consumer independently decided whether
it was "waiting". When two flows overlapped, whichever handler sat earliest in
`ai_handler` consumed the text for the stale flow — e.g. a section name typed
for Section Create also triggered the leftover contribution-review note.

The fix is one marker of the currently active workflow. Starting a flow
cancels every other flow's pending keys and claims the marker; a text
consumer may only take the message when it owns (or when no flow is active,
which keeps direct/legacy calls working). A message is therefore consumed by
exactly the workflow the user is actually in.

Keys are grouped per workflow because a flow may use more than one key (the
upload flow, for instance, owns both its "awaiting media" flag and its
"awaiting custom title" flag). Re-entering the same flow is idempotent.
"""

ACTIVE_KEY = "active_workflow"

# Each workflow -> the user_data keys it owns while active.
WORKFLOWS = {
    "admin_upload": (
        "admin_upload",
        "admin_upload_folder",
        "admin_upload_preview",
        "admin_upload_waiting_title",
        "admin_upload_title",
    ),
    "admin_file_rename": (
        "admin_file_rename",
        "admin_file_rename_id",
        "admin_file_rename_waiting",
    ),
    "admin_folder_create": (
        "admin_folder_create",
        "admin_folder_parent",
        "admin_folder_name",
        "admin_folder_type",
    ),
    "admin_folder_rename": (
        "admin_folder_rename",
        "admin_folder_rename_id",
    ),
    "admin_folder_move": (
        "admin_folder_move",
        "admin_folder_move_id",
    ),
    "admin_file_move": (
        "admin_file_move",
        "admin_file_move_id",
    ),
    "admin_folder_retype": ("admin_folder_retype_id",),
    "review_note": ("review_note_kind", "review_note_id"),
    "contact_message": ("contact_category",),
    "admin_reply": ("contact_reply_id",),
    "admin_add": ("admin_mgmt_waiting_add",),
    "settings_edit": ("settings_edit_key",),
    "topics_create": ("topics_create",),
    "topics_link": ("topics_link_id",),
    "notification_body": ("notifications_body",),
}


def begin(context, name: str) -> None:
    """Make `name` the single active workflow, cancelling any other.

    The other workflows' keys are removed, so their text consumers can no
    longer accidentally consume input meant for the new flow.
    """
    if name not in WORKFLOWS:
        return

    for other, keys in WORKFLOWS.items():
        if other == name:
            continue
        for key in keys:
            context.user_data.pop(key, None)

    context.user_data[ACTIVE_KEY] = name


def owns(context, name: str) -> bool:
    """True when `name` may consume the pending message.

    When no workflow is marked active (a direct handler call, or a flow armed
    before this registry existed), ownership is not contested and the caller's
    own state check decides — preserving the pre-existing contract.
    """
    active = context.user_data.get(ACTIVE_KEY)
    return active is None or active == name


def clear(context) -> None:
    """Clear the active workflow and its own pending keys."""
    name = context.user_data.pop(ACTIVE_KEY, None)
    for key in WORKFLOWS.get(name, ()):
        context.user_data.pop(key, None)


def clear_all(context) -> None:
    """Clear every workflow's pending keys and the marker."""
    context.user_data.pop(ACTIVE_KEY, None)
    for keys in WORKFLOWS.values():
        for key in keys:
            context.user_data.pop(key, None)
