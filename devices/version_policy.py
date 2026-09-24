"""Mobile Version Policy — enforcement middleware + shared helpers.

Two mobile platforms (ANDROID, IOS) can be version-gated; the web never is.
The middleware reads the version headers the mobile client already sends
(``X-Platform`` / ``X-Build-Number`` / ``X-App-Version``) and, when an active
:class:`~devices.models.VersionPolicy` exists for that platform, requires the
client's build to be AT LEAST the required build. A client below it gets HTTP
426 with an ``APP_UPDATE_REQUIRED`` body so the app can show an update screen.
Clearing the required build switches the gate off for that platform.

Kept out of ``models.py`` / ``admin_views.py`` so the enforcement path is small
and self-contained.
"""
import json
import logging

from django.core.cache import cache

from .models import PLATFORM_ANDROID, PLATFORM_IOS, VersionPolicy

logger = logging.getLogger(__name__)

# The only platforms that are ever gated. A request whose X-Platform is anything
# else (WEB, DESKTOP, missing, junk) is passed straight through.
GATED_PLATFORMS = frozenset({PLATFORM_ANDROID, PLATFORM_IOS})

# Active policies change rarely (an admin edits them by hand), but the
# middleware runs on every mobile request — so cache the lookup briefly rather
# than hitting the DB each time. Short TTL so a policy edit takes effect within
# ~30s without a manual cache clear.
_CACHE_KEY = "devices:version_policy:active"
_CACHE_TTL_SECONDS = 30


def active_policies():
    """Return ``{platform: {required_build, required_version, store_url}}`` for
    every currently-active mobile policy. Cached briefly."""
    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return cached
    policies = {
        p.platform: {
            "required_build": p.required_build,
            "required_version": p.required_version,
            "store_url": p.store_url,
        }
        for p in VersionPolicy.objects.filter(
            is_active=True, platform__in=(PLATFORM_ANDROID, PLATFORM_IOS)
        )
    }
    cache.set(_CACHE_KEY, policies, _CACHE_TTL_SECONDS)
    return policies


def clear_policy_cache():
    """Drop the cached lookup so the next request re-reads the DB. Called from
    the admin write path after any policy change."""
    cache.delete(_CACHE_KEY)


def evaluate(platform, build_number, app_version, policies=None):
    """Return an ``APP_UPDATE_REQUIRED`` payload dict if the client is out of
    policy, else ``None`` (allowed through).

    Rules, in order:
      * platform not gated (web/desktop/unknown)  -> None
      * no active policy for the platform         -> None (nothing to enforce)
      * policy has no required_build (blank)      -> None (gate switched off)
      * build >= required_build                   -> None (up to date)
      * otherwise                                 -> update-required payload

    The comparison is a MINIMUM on the build number, and the version label is
    never compared. Both of those are fixes, not preferences:

    * This was `build == required`, so a device on a NEWER build than the
      policy was told to update -- with nothing newer to install, it could
      never clear. Anyone who took a Play Store update before the admin bumped
      the policy was locked out of the app entirely.
    * `required_version` was also compared as a STRING, so "1.0.10" != "1.0.9"
      blocked a device that was ahead on both counts. The version is a label
      for humans; the build is the orderable one.
    """
    if platform not in GATED_PLATFORMS:
        return None

    policies = active_policies() if policies is None else policies
    policy = policies.get(platform)
    if not policy:
        return None

    # A blank required_build switches the gate off for this platform, letting
    # every version through. That is the intended escape hatch: an admin clears
    # the field when a bad policy is locking people out.
    required_build = policy.get("required_build")
    if required_build in (None, ""):
        return None

    # The client's build must parse to an int to be comparable. A missing or
    # unparseable build fails the check (treated as below the floor) rather
    # than being waved through — the header is required for gated apps.
    try:
        client_build = int(str(build_number).strip())
    except (TypeError, ValueError):
        client_build = None

    if client_build is not None and client_build >= int(required_build):
        return None

    return {
        "success": False,
        "code": "APP_UPDATE_REQUIRED",
        "message": "Please update your application.",
        "store_url": policy["store_url"],
        "required_version": policy["required_version"],
        "required_build": policy["required_build"],
    }


class VersionPolicyMiddleware:
    """Blocks out-of-policy mobile clients with HTTP 426.

    Runs on every request but does real work only for gated mobile platforms
    that have an active policy. The web (and any request without mobile version
    headers) is never touched.
    """

    STATUS_UPGRADE_REQUIRED = 426

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        platform = (request.META.get("HTTP_X_PLATFORM") or "").strip().upper()

        # Fast path: not a gated mobile platform -> do nothing, cost is one dict
        # lookup. This is the web's path, and the vast majority of traffic.
        if platform in GATED_PLATFORMS:
            payload = evaluate(
                platform,
                request.META.get("HTTP_X_BUILD_NUMBER"),
                request.META.get("HTTP_X_APP_VERSION"),
            )
            if payload is not None:
                logger.info(
                    "Blocked out-of-date %s client (build=%s version=%s)",
                    platform,
                    request.META.get("HTTP_X_BUILD_NUMBER"),
                    request.META.get("HTTP_X_APP_VERSION"),
                )
                from django.http import HttpResponse

                return HttpResponse(
                    json.dumps(payload),
                    status=self.STATUS_UPGRADE_REQUIRED,
                    content_type="application/json",
                )

        return self.get_response(request)
