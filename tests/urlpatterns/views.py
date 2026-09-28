from django.http import HttpResponse


def empty_view(request, *args, **kwargs):
    return HttpResponse()


def old_view(request, *args, **kwargs):
    return HttpResponse()


def new_view(request, *args, **kwargs):
    return HttpResponse()
