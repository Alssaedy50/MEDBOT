"""Central scoped authorization service (Phase 3 — Scoped RBAC).

This is the ONLY place that combines a *capability* (does this admin hold the
coarse permission at all?) with a *scope* (is the target inside the branch they
are responsible for?). Handlers never re-implement that decision; they call
`can()` (or `require()`) and act on the boolean.

Model (matches the product vision):

    Owner / Super Admin  -> full access, every scope.
    Admin                -> a set of permissions; optionally restricted to a
                            set of scopes (subject / folder / resource).
    Default              -> DENY.

Decision order for `can(user_id, permission, target_type, target_id)`:

    1. Is the actor an active admin?                 no  -> DENY
    2. Is the operation a known scoped permission?   no  -> DENY
    3. Does the actor's role clear the coarse key?   no  -> DENY
       (owner always clears it; a legacy admin with empty permissions keeps
       full access exactly as before this phase)
    4. Is the actor scope-restricted?
         - no scope rows -> platform-wide, handled like (3) -> ALLOW
         - has scope rows -> ALLOW only if the target resolves inside a scope;
           no target, unknown target, deleted target -> DENY (fail-closed)

The scope layer is *opt-in*: an admin with no scope rows keeps the pre-Phase-3
platform-wide reach, so installing this phase revokes nothing by itself.

Scope semantics (target_type -> tester):

    folder       -> target folder is at/beneath a folder scope, or inside a
                    topic scope's linked folders
    topic        -> the admin holds a `topic` scope on this exact topic
    resource     -> the resource is named by a `resource` scope, or its real
                    folder falls inside a folder/topic scope
    news         -> any real folder the item references (subject/section/anchor
                    or its resource's folder) falls inside a scope
    contribution -> the contribution's destination folder falls inside a scope
    (none/global)-> never inside a scope (so a scoped admin cannot broadcast
                    globally); only an unscoped admin or the owner may.

Anything not listed is treated as "no target", which a scope-restricted admin
can never satisfy.
"""

import logging

import database

logger = logging.getLogger(__name__)

# Outcome codes for `require()` so callers can distinguish a missing capability
# (surface hidden) from an out-of-scope target (surface shown, action denied).
DECISION_ALLOW = "allow"
DECISION_NO_PERMISSION = "no_permission"
DECISION_OUT_OF_SCOPE = "out_of_scope"
DECISION_NOT_ADMIN = "not_admin"
DECISION_UNKNOWN = "unknown_permission"


def _int_or_none(value):
    try:
        if value is None or value is True or value is False:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


async def is_admin(user_id) -> bool:
    """Active admin (owner / admin / reviewer) — the coarse gate."""
    try:
        return await database.is_user_admin(user_id)
    except Exception:
        return False


async def is_owner(user_id) -> bool:
    try:
        return await database.is_owner(user_id)
    except Exception:
        return False


async def _target_in_scope(admin_id, target_type, target_id):
    """True when (target_type, target_id) resolves inside the admin's scopes."""
    if not target_type:
        return False

    if target_type == "folder":
        return await database.folder_in_admin_scope(admin_id, target_id)

    if target_type == "topic":
        return await database.is_topic_in_admin_scope(admin_id, target_id)

    if target_type == "resource":
        return await database.resource_in_admin_scope(admin_id, target_id)

    if target_type == "news":
        # Any real folder the item references (subject/section/anchor or its
        # resource's folder) inside a folder/topic scope allows it.
        folders = await database.news_scope_folder_ids(target_id)
        for folder_id in folders:
            if await database.folder_in_admin_scope(admin_id, folder_id):
                return True
        # Its referenced resource may also be a direct `resource` scope.
        resource_id = await database.news_resource_id(target_id)
        if resource_id is not None:
            return await database.resource_in_admin_scope(admin_id, resource_id)
        return False

    if target_type == "contribution":
        folder_id = await database.contribution_folder_id(target_id)
        if folder_id is None:
            return False
        return await database.folder_in_admin_scope(admin_id, folder_id)

    # Unknown/global target: never inside a scope (fail-closed).
    return False


async def can(user_id, permission, target_type=None, target_id=None) -> bool:
    """The single authorization predicate. Deny-by-default.

    See the module docstring for the decision order. Never raises.
    """
    try:
        if permission not in database.SCOPED_PERMISSIONS:
            return False

        record = await database.get_admin_record(user_id)
        if not record or record.get("role") in (None, "none"):
            return False

        if record["role"] == "owner":
            return True

        coarse = database.SCOPED_PERMISSION_COARSE.get(permission)
        if not coarse:
            return False
        if not record["permissions"].get(coarse):
            return False

        # Scope restriction is opt-in: no rows means platform-wide.
        if not await database.admin_has_scopes(user_id):
            return True

        return await _target_in_scope(user_id, target_type, target_id)
    except Exception:
        logger.exception("authorization.can failed; denying")
        return False


async def require(user_id, permission, target_type=None, target_id=None):
    """Like `can`, but returns a DECISION_* code and audits a denial.

    Callers that render a screen use this to show the right message (hidden
    capability vs. out-of-scope target). Auditing is best-effort and never
    raises into the caller.
    """
    if permission not in database.SCOPED_PERMISSIONS:
        return DECISION_UNKNOWN

    try:
        record = await database.get_admin_record(user_id)
    except Exception:
        record = None

    if not record or record.get("role") in (None, "none"):
        await _audit_denial(user_id, permission, target_type, target_id,
                            DECISION_NOT_ADMIN)
        return DECISION_NOT_ADMIN

    if record["role"] == "owner":
        return DECISION_ALLOW

    coarse = database.SCOPED_PERMISSION_COARSE.get(permission)
    if not coarse or not record["permissions"].get(coarse):
        await _audit_denial(user_id, permission, target_type, target_id,
                            DECISION_NO_PERMISSION)
        return DECISION_NO_PERMISSION

    try:
        if not await database.admin_has_scopes(user_id):
            return DECISION_ALLOW
        if await _target_in_scope(user_id, target_type, target_id):
            return DECISION_ALLOW
    except Exception:
        logger.exception("authorization.require scope check failed")

    await _audit_denial(user_id, permission, target_type, target_id,
                        DECISION_OUT_OF_SCOPE)
    return DECISION_OUT_OF_SCOPE


async def _audit_denial(user_id, permission, target_type, target_id, decision):
    try:
        import audit

        await audit.log_action(
            user_id,
            "authz_denied",
            target_type=target_type,
            target_id=target_id,
            details=f"permission={permission};decision={decision}",
        )
    except Exception:
        pass


def decision_allowed(decision) -> bool:
    return decision == DECISION_ALLOW


# ------------------------------------------------------------
# Convenience wrappers for the common call sites (no logic duplicated here —
# each is a thin `can` with the declared coarse capability + target).
# ------------------------------------------------------------
async def can_manage_resource(user_id, action, content_id) -> bool:
    """action in {'view','create','edit','delete'} against a content row."""
    return await can(user_id, f"resource.{action}", "resource", content_id)


async def can_create_resource_in_folder(user_id, folder_id) -> bool:
    return await can(user_id, "resource.create", "folder", folder_id)


async def can_manage_section(user_id, folder_id) -> bool:
    return await can(user_id, "section.manage", "folder", folder_id)


async def can_manage_news(user_id, action, news_id) -> bool:
    """action in {'view','edit','publish','archive'} against a news row."""
    return await can(user_id, f"news.{action}", "news", news_id)


async def can_create_news_in_folder(user_id, folder_id) -> bool:
    return await can(user_id, "news.create", "folder", folder_id)


async def can_review_contribution(user_id, contribution_id) -> bool:
    return await can(user_id, "contribution.review", "contribution", contribution_id)


async def can_send_notification(user_id) -> bool:
    """Global broadcast has no folder target, so any scope restriction denies."""
    return await can(user_id, "notification.send")


async def can_manage_ai_registry(user_id, topic_id=None) -> bool:
    return await can(user_id, "ai_registry.manage", "topic", topic_id)


# ------------------------------------------------------------
# List filtering helpers for admin surfaces
# ------------------------------------------------------------
async def scoped_folder_ids(user_id):
    """Subtree folder ids the admin may see, or None when unrestricted.

    None means "platform-wide" (owner / unscoped admin); callers treat it as
    "no filter". An empty set means "restricted but no reachable folder".
    """
    try:
        if await database.is_owner(user_id):
            return None
        if not await database.admin_has_scopes(user_id):
            return None
        roots = await database.topic_folder_roots(user_id)
        return await database.list_folder_ids_under(roots)
    except Exception:
        logger.exception("authorization.scoped_folder_ids failed")
        return set()
