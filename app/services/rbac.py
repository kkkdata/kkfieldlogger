from __future__ import annotations

from app.models import User, UserRole


PLATFORM_ADMIN_ROLES = {UserRole.platform_super_admin}
TENANT_ADMIN_ROLES = {
    UserRole.platform_super_admin,
    UserRole.super_admin,
    UserRole.owner,
    UserRole.admin,
}
MANAGER_ROLES = TENANT_ADMIN_ROLES | {UserRole.project_manager, UserRole.manager}
CLIENT_ROLES = {UserRole.client, UserRole.client_viewer}
WORKER_ROLES = {UserRole.worker, UserRole.employee}


def is_platform_admin(user: User) -> bool:
    return user.role in PLATFORM_ADMIN_ROLES


def can_manage_companies(user: User) -> bool:
    # Platform-level powers (create/approve/delete tenants) are reserved for
    # platform_super_admin. Tenant super_admin accounts stay tenant-scoped;
    # existing platform operators are migrated by 20260818_0044.
    return user.role in PLATFORM_ADMIN_ROLES


def is_tenant_admin(user: User) -> bool:
    return user.role in TENANT_ADMIN_ROLES


def is_manager(user: User) -> bool:
    return user.role in MANAGER_ROLES


def is_client(user: User) -> bool:
    return user.role in CLIENT_ROLES


def is_worker(user: User) -> bool:
    return user.role in WORKER_ROLES


def role_matches(user: User, *roles: UserRole) -> bool:
    expanded: set[UserRole] = set()
    for role in roles:
        expanded.add(role)
        if role == UserRole.super_admin:
            expanded.update(TENANT_ADMIN_ROLES)
        elif role == UserRole.project_manager:
            expanded.update({UserRole.project_manager, UserRole.manager})
        elif role == UserRole.client:
            expanded.update(CLIENT_ROLES)
        elif role == UserRole.worker:
            expanded.update(WORKER_ROLES)
    return user.role in expanded
