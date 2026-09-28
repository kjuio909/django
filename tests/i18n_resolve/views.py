"""Views used by the i18n URL resolution tests."""


def empty_view(request, *args, **kwargs):
    pass


def simple_view(request):
    pass


def bounded_view(request, name):
    pass


def item_view(request, pk):
    pass


def legacy_view(request, legacy_id):
    pass


def plain_view(request, pk):
    pass
