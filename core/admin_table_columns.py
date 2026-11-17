"""Column layouts for the "All Users & Devices" tables.

Every tab of ``android_app_users_full`` is a wide table, and which columns an
operator cares about differs from person to person (an ops admin watches
heartbeats, a billing admin watches plans). This module is the single place
that knows:

* which columns each tab has (``TAB_SPECS``),
* which ones a given admin has chosen and in what order
  (``AdminTableLayout``, one row per admin + tab),
* how a clickable header sorts that tab's queryset (``SORT_FIELDS``).

The templates render every column the tab has; the browser then hides and
reorders them from the saved layout (see ``applyTableLayout`` in
``android_app_users_full.html``), so changing the layout never needs a
migration of the markup, only one saved list of keys.
"""

from .models import AdminTableLayout

# Fallback wording when a tab has no saved layout: this is what a fresh admin
# sees, and the order the table markup is written in.
TAB_SPECS = {
    'accounts': [
        {'key': 'user', 'label': 'User'},
        {'key': 'status', 'label': 'Status'},
        {'key': 'device', 'label': 'Device (live)'},
        {'key': 'app_device', 'label': 'App device record'},
        {'key': 'account', 'label': 'Account'},
        {'key': 'subscription', 'label': 'Subscription'},
        {'key': 'library', 'label': 'Library / cloud'},
        {'key': 'access', 'label': 'Access'},
        {'key': 'manage', 'label': 'Manage'},
    ],
    'registered': [
        {'key': 'user', 'label': 'User', 'sort': 'display_name'},
        {'key': 'email', 'label': 'Email', 'sort': 'email'},
        {'key': 'verified', 'label': 'Email verified', 'sort': 'is_email_verified'},
        {'key': 'joined', 'label': 'Registered', 'sort': 'created_at'},
        {'key': 'plan', 'label': 'Plan', 'sort': 'plan'},
        {'key': 'valid', 'label': 'Paid until', 'sort': 'valid_until'},
        {'key': 'status', 'label': 'Block state', 'sort': 'block_mode'},
        {'key': 'device', 'label': 'Device', 'sort': 'device_model'},
        {'key': 'app', 'label': 'App version', 'sort': 'app_version'},
        {'key': 'build', 'label': 'Build', 'sort': 'build_number'},
        {'key': 'last_sync', 'label': 'Last sync', 'sort': 'last_synced_at'},
        {'key': 'library', 'label': 'Library / cloud'},
        {'key': 'manage', 'label': 'Manage'},
    ],
    'versions': [
        {'key': 'user', 'label': 'User'},
        {'key': 'app_version', 'label': 'App version'},
        {'key': 'build', 'label': 'Build'},
        {'key': 'status', 'label': 'Status'},
        {'key': 'device', 'label': 'Device'},
        {'key': 'last_sync', 'label': 'Last synced'},
        {'key': 'plan', 'label': 'Plan'},
    ],
    'uniquedevices': [
        {'key': 'index', 'label': '#'},
        {'key': 'device', 'label': 'Device'},
        {'key': 'account', 'label': 'Account'},
        {'key': 'status', 'label': 'Status'},
        {'key': 'ip', 'label': 'IP'},
        {'key': 'subscription', 'label': 'Subscription'},
        {'key': 'first_registered', 'label': 'First registered'},
        {'key': 'last_seen', 'label': 'Last seen online'},
        {'key': 'visits', 'label': 'Visits'},
        {'key': 'history', 'label': 'History'},
    ],
    'inactive': [
        {'key': 'check', 'label': ''},
        {'key': 'device', 'label': 'Device'},
        {'key': 'account', 'label': 'Account'},
        {'key': 'last_seen', 'label': 'Last seen'},
        {'key': 'silent', 'label': 'Silent'},
        {'key': 'visits', 'label': 'Visits'},
        {'key': 'first_seen', 'label': 'First seen'},
        {'key': 'actions', 'label': ''},
    ],
    'signedout': [
        {'key': 'account', 'label': 'Account'},
        {'key': 'online', 'label': 'Online & IP'},
        {'key': 'app', 'label': 'App & device'},
        {'key': 'doing', 'label': 'What they are doing'},
        {'key': 'last_seen', 'label': 'Last seen'},
        {'key': 'actions', 'label': ''},
    ],
    'visits': [
        {'key': 'time', 'label': 'Time'},
        {'key': 'device_id', 'label': 'Device ID'},
        {'key': 'account', 'label': 'Account'},
        {'key': 'model', 'label': 'Model'},
        {'key': 'os', 'label': 'OS'},
        {'key': 'build', 'label': 'Build ID'},
        {'key': 'ip', 'label': 'IP address'},
        {'key': 'request', 'label': 'Request'},
    ],
    'dailyusers': [
        {'key': 'period', 'label': 'Period'},
        {'key': 'active', 'label': 'Active'},
        {'key': 'new', 'label': 'New'},
        {'key': 'registered', 'label': 'Registered'},
        {'key': 'converted', 'label': 'Converted'},
        {'key': 'share', 'label': 'Share of the peak'},
        {'key': 'change', 'label': 'Change'},
    ],
}

# ORM fields a header click may sort by. A column that is not listed here is
# not sortable (it is computed per row, or it is an action cell).
SORT_FIELDS = {
    'registered': {
        'display_name': 'display_name',
        'email': 'email',
        'is_email_verified': 'is_email_verified',
        'created_at': 'created_at',
        'plan': 'plan',
        'valid_until': 'valid_until',
        'block_mode': 'block_mode',
        'device_model': 'device_model',
        'app_version': 'app_version',
        'build_number': 'build_number',
        'last_synced_at': 'last_synced_at',
    },
}

DEFAULT_SORT = {'registered': '-last_synced_at'}


def specs(tab):
    """Every column a tab can show, in its default order."""
    return [dict(col) for col in TAB_SPECS.get(tab, [])]


def default_keys(tab):
    return [col['key'] for col in TAB_SPECS.get(tab, [])]


def _valid_keys(tab, keys):
    """Keep only keys the tab actually has, drop repeats, keep the given order."""
    known = default_keys(tab)
    out = []
    for key in keys or []:
        key = str(key).strip()
        if key in known and key not in out:
            out.append(key)
    return out


def saved_keys(user, tab):
    """The admin's own column order for this tab, or the tab's default order."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return default_keys(tab)
    try:
        row = (AdminTableLayout.objects.filter(user=user, tab=tab)
               .values_list('columns', flat=True).first())
    except Exception:
        # A layout is a preference: if its table is missing (code deployed
        # ahead of `migrate`), the page still has to render with the defaults.
        row = None
    keys = _valid_keys(tab, row)
    return keys or default_keys(tab)


def save_keys(user, tab, keys):
    """Store one admin's column order for one tab and return it back."""
    keys = _valid_keys(tab, keys) or default_keys(tab)
    AdminTableLayout.objects.update_or_create(
        user=user, tab=tab, defaults={'columns': keys})
    return keys


def columns_for(user, tab, sort_key='', sort_dir='asc', base_params=None):
    """Columns to render: label, whether it can sort, and its header link.

    ``base_params`` is a dict of the current query string values the sort link
    has to keep (tab, q, filters). The template only has to print ``url``.
    """
    saved = saved_keys(user, tab)
    sortable = SORT_FIELDS.get(tab, {})
    params = dict(base_params or {})
    columns = []
    for col in specs(tab):
        field = sortable.get(col.get('sort') or '')
        entry = {
            'key': col['key'],
            'label': col['label'],
            'visible': col['key'] in saved,
            'sortable': bool(field),
            'active': bool(field) and sort_key == (col.get('sort') or ''),
            'dir': sort_dir,
        }
        if field:
            nxt = 'desc' if entry['active'] and sort_dir == 'asc' else 'asc'
            link = dict(params)
            link['tab'] = tab
            link['sort'] = col['sort']
            link['dir'] = nxt
            link.pop('page', None)
            entry['url'] = '&'.join(
                f'{k}={v}' for k, v in link.items() if v not in (None, ''))
        columns.append(entry)
    return columns


def parse_sort(tab, sort_key, sort_dir):
    """Validate ?sort=/?dir= against the tab and return (key, direction)."""
    sortable = SORT_FIELDS.get(tab, {})
    key = (sort_key or '').strip()
    direction = 'desc' if (sort_dir or '').strip().lower() == 'desc' else 'asc'
    if key not in sortable:
        default = DEFAULT_SORT.get(tab, '')
        if default.startswith('-'):
            return default[1:], 'desc'
        return default, 'asc'
    return key, direction


def order_queryset(qs, tab, sort_key, sort_dir):
    """Apply the validated sort to a queryset (no-op when unsortable)."""
    field = SORT_FIELDS.get(tab, {}).get(sort_key or '')
    if not field:
        return qs
    prefix = '-' if sort_dir == 'desc' else ''
    # A stable tie-breaker keeps pagination from repeating rows.
    return qs.order_by(prefix + field, '-id')


def table_cells_attr(tab):
    """``data-tab-cols`` value: the markup's column order, comma separated."""
    return ','.join(default_keys(tab))
