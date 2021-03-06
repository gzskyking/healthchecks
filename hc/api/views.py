from datetime import timedelta as td

from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.db import connection
from django.http import (HttpResponse, HttpResponseForbidden,
                         HttpResponseNotFound, JsonResponse)
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from hc.api import schemas
from hc.api.decorators import authorize, authorize_read, validate_json
from hc.api.models import Check, Notification, Channel
from hc.lib.badges import check_signature, get_badge_svg


@csrf_exempt
@never_cache
def ping(request, code, is_fail=False):
    check = get_object_or_404(Check, code=code)

    headers = request.META
    remote_addr = headers.get("HTTP_X_FORWARDED_FOR", headers["REMOTE_ADDR"])
    remote_addr = remote_addr.split(",")[0]
    scheme = headers.get("HTTP_X_FORWARDED_PROTO", "http")
    method = headers["REQUEST_METHOD"]
    ua = headers.get("HTTP_USER_AGENT", "")
    body = request.body.decode()

    check.ping(remote_addr, scheme, method, ua, body, is_fail)

    response = HttpResponse("OK")
    response["Access-Control-Allow-Origin"] = "*"
    return response


def _lookup(user, spec):
    unique_fields = spec.get("unique", [])
    if unique_fields:
        existing_checks = Check.objects.filter(user=user)
        if "name" in unique_fields:
            existing_checks = existing_checks.filter(name=spec.get("name"))
        if "tags" in unique_fields:
            existing_checks = existing_checks.filter(tags=spec.get("tags"))
        if "timeout" in unique_fields:
            timeout = td(seconds=spec["timeout"])
            existing_checks = existing_checks.filter(timeout=timeout)
        if "grace" in unique_fields:
            grace = td(seconds=spec["grace"])
            existing_checks = existing_checks.filter(grace=grace)

        return existing_checks.first()


def _update(check, spec):
    if "name" in spec:
        check.name = spec["name"]

    if "tags" in spec:
        check.tags = spec["tags"]

    if "timeout" in spec and "schedule" not in spec:
        check.kind = "simple"
        check.timeout = td(seconds=spec["timeout"])

    if "grace" in spec:
        check.grace = td(seconds=spec["grace"])

    if "schedule" in spec:
        check.kind = "cron"
        check.schedule = spec["schedule"]
        if "tz" in spec:
            check.tz = spec["tz"]

    check.save()

    # This needs to be done after saving the check, because of
    # the M2M relation between checks and channels:
    if "channels" in spec:
        if spec["channels"] == "*":
            check.assign_all_channels()
        elif spec["channels"] == "":
            check.channel_set.clear()
        else:
            channels = []
            for chunk in spec["channels"].split(","):
                try:
                    channel = Channel.objects.get(code=chunk)
                    channels.append(channel)
                except Channel.DoesNotExist:
                    raise SuspiciousOperation("Invalid channel identifier")
            check.channel_set.set(channels)

    return check


@validate_json()
@authorize_read
def get_checks(request):
    q = Check.objects.filter(user=request.user)

    tags = set(request.GET.getlist("tag"))
    for tag in tags:
        # approximate filtering by tags
        q = q.filter(tags__contains=tag)

    checks = []
    for check in q:
        # precise, final filtering
        if not tags or check.matches_tag_set(tags):
            checks.append(check.to_dict())

    return JsonResponse({"checks": checks})


@validate_json(schemas.check)
@authorize
def create_check(request):
    created = False
    check = _lookup(request.user, request.json)
    if check is None:
        num_checks = Check.objects.filter(user=request.user).count()
        if num_checks >= request.user.profile.check_limit:
            return HttpResponseForbidden()

        check = Check(user=request.user)
        created = True

    _update(check, request.json)
    return JsonResponse(check.to_dict(), status=201 if created else 200)


@csrf_exempt
def checks(request):
    if request.method == "GET":
        return get_checks(request)

    elif request.method == "POST":
        return create_check(request)

    return HttpResponse(status=405)


@csrf_exempt
@validate_json(schemas.check)
@authorize
def update(request, code):
    check = get_object_or_404(Check, code=code)
    if check.user != request.user:
        return HttpResponseForbidden()

    if request.method == "POST":
        _update(check, request.json)
        return JsonResponse(check.to_dict())

    elif request.method == "DELETE":
        response = check.to_dict()
        check.delete()
        return JsonResponse(response)

    # Otherwise, method not allowed
    return HttpResponse(status=405)


@csrf_exempt
@require_POST
@validate_json()
@authorize
def pause(request, code):
    check = get_object_or_404(Check, code=code)
    if check.user != request.user:
        return HttpResponseForbidden()

    check.status = "paused"
    check.save()
    return JsonResponse(check.to_dict())


@never_cache
def badge(request, username, signature, tag, format="svg"):
    if not check_signature(username, tag, signature):
        return HttpResponseNotFound()

    status = "up"
    q = Check.objects.filter(user__username=username)
    if tag != "*":
        q = q.filter(tags__contains=tag)
        label = tag
    else:
        label = settings.MASTER_BADGE_LABEL

    for check in q:
        if tag != "*" and tag not in check.tags_list():
            continue

        check_status = check.get_status()
        if status == "up" and check_status == "grace":
            status = "late"

        if check_status == "down":
            status = "down"
            break

    if format == "json":
        return JsonResponse({"status": status})

    svg = get_badge_svg(label, status)
    return HttpResponse(svg, content_type="image/svg+xml")


@csrf_exempt
def bounce(request, code):
    notification = get_object_or_404(Notification, code=code)

    # If webhook is more than 10 minutes late, don't accept it:
    td = timezone.now() - notification.created
    if td.total_seconds() > 600:
        return HttpResponseForbidden()

    notification.error = request.body.decode()[:200]
    notification.save()

    notification.channel.email_verified = False
    notification.channel.save()

    return HttpResponse()


def status(request):
    with connection.cursor() as c:
        c.execute("SELECT 1")
        c.fetchone()

    return HttpResponse("OK")
